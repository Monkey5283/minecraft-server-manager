from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import Counter
from pathlib import Path

import discord
from discord import app_commands

from .client import AgentClient, AgentUnavailable
from .config import ControllerConfig, RemoteServer
from .health import ControllerHealthSnapshot, HealthLevel
from .ups import (
    format_charge_percent,
    is_ups_ready,
    ups_power_label,
    ups_status_message,
)


LOG = logging.getLogger("mc_manager.discord")
STATUS_ICONS = {
    "online": "🟢",
    "offline": "🔴",
    "busy": "🟡",
    "unreachable": "⚫",
    "unknown": "⚪",
}


class MinecraftDiscordBot(discord.Client):
    def __init__(
        self,
        config: ControllerConfig,
        agents: AgentClient,
        *,
        health_state_file: Path | None = None,
    ):
        intents = discord.Intents.default()
        client_options = {}
        if config.health_presence_enabled:
            client_options = {
                "activity": discord.Activity(
                    type=discord.ActivityType.watching,
                    name="Minecraft servers • Checking",
                ),
                "status": discord.Status.idle,
            }
        super().__init__(intents=intents, **client_options)
        self.config = config
        self.agents = agents
        self.servers = {server.id: server for server in config.servers}
        self._online_announced = False
        self._health_state_file = health_state_file
        self._ups_card_channel_id: int | None = None
        self._ups_card_message_id: int | None = None
        self._ups_card_signature: tuple | None = None
        self._ups_card_state_dirty = False
        self._ups_card_lock = asyncio.Lock()
        self._presence_level: HealthLevel | None = None
        self._desired_presence_level: HealthLevel | None = None
        self._presence_lock = asyncio.Lock()
        self._load_ups_card_state()
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def is_allowed(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if user.id in self.config.allowed_user_ids:
            return True
        if isinstance(user, discord.Member):
            if user.guild_permissions.administrator:
                return True
            return bool(
                self.config.allowed_role_ids.intersection(role.id for role in user.roles)
            )
        return False

    @staticmethod
    def is_administrator(interaction: discord.Interaction) -> bool:
        user = interaction.user
        return (
            isinstance(user, discord.Member)
            and user.guild_permissions.administrator
        )

    async def server_autocomplete(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [
            app_commands.Choice(name=server.name, value=server.id)
            for server in self.config.servers
            if current in server.name.lower() or current in server.id
        ][:25]

    async def script_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        server_id = getattr(interaction.namespace, "server", None)
        server = self.servers.get(server_id)
        if not server:
            return []
        try:
            status = await self.agents.status(server)
        except AgentUnavailable:
            return []
        return [
            app_commands.Choice(name=name, value=name)
            for name in status.get("scripts", [])
            if current.lower() in name.lower()
        ][:25]

    def _register_commands(self) -> None:
        @self.tree.command(
            name="minecraft",
            description="Manage a Minecraft server",
        )
        @app_commands.describe(
            action="What to do",
            server="Server to manage",
            script="Allowlisted script name (only used with run-script)",
        )
        @app_commands.choices(
            action=[
                app_commands.Choice(name="Status", value="status"),
                app_commands.Choice(name="Start", value="start"),
                app_commands.Choice(name="Stop", value="stop"),
                app_commands.Choice(name="Restart", value="restart"),
                app_commands.Choice(name="Apply update", value="update"),
                app_commands.Choice(name="Run script", value="run-script"),
            ]
        )
        @app_commands.autocomplete(
            server=self.server_autocomplete,
            script=self.script_autocomplete,
        )
        async def minecraft(
            interaction: discord.Interaction,
            action: app_commands.Choice[str],
            server: str,
            script: str | None = None,
        ) -> None:
            if not self.is_allowed(interaction):
                await interaction.response.send_message(
                    "You are not allowed to manage these servers.", ephemeral=True
                )
                return
            target = self.servers.get(server)
            if not target:
                await interaction.response.send_message(
                    "Unknown server.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                if action.value == "status":
                    result = await self.agents.status(target)
                    await interaction.followup.send(
                        f"**{target.name}** is **{result['state']}**.", ephemeral=True
                    )
                    return
                if action.value == "run-script":
                    if not script:
                        await interaction.followup.send(
                            "Choose an allowlisted script.", ephemeral=True
                        )
                        return
                    job = await self.agents.script(target, script)
                else:
                    job = await self.agents.action(target, action.value)
                result = await self._wait_for_job(target, job["id"])
                message = self._job_result_message(target, result, job["id"])
                await interaction.followup.send(message, ephemeral=True)
            except AgentUnavailable as exc:
                await interaction.followup.send(str(exc), ephemeral=True)

        @self.tree.command(
            name="status",
            description="Show the status of every Minecraft server",
        )
        async def status(interaction: discord.Interaction) -> None:
            if not self.is_administrator(interaction):
                await interaction.response.send_message(
                    "Only Discord administrators can use `/status`.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(
                await self._server_status_message(),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @self.tree.command(
            name="players",
            description="Show active Minecraft players and their server",
        )
        async def players(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(
                await self._players_message(),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @self.tree.command(
            name="ups",
            description="Show the controller battery backup status",
        )
        async def ups(interaction: discord.Interaction) -> None:
            if not self.is_allowed(interaction):
                await interaction.response.send_message(
                    "You are not allowed to view UPS status.", ephemeral=True
                )
                return
            if not self.config.ups.enabled:
                await interaction.response.send_message(
                    "UPS monitoring is not enabled on this controller.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await interaction.followup.send(
                    await ups_status_message(self.config.ups),
                    ephemeral=True,
                )
            except Exception as exc:
                LOG.exception("Could not read UPS status for Discord command")
                await interaction.followup.send(
                    f"Could not read UPS status: {exc}",
                    ephemeral=True,
                )

    async def _wait_for_job(
        self, server: RemoteServer, job_id: str, attempts: int = 120
    ) -> dict:
        for _ in range(attempts):
            job = await self.agents.job(server, job_id)
            if job["state"] in {"succeeded", "failed"}:
                return job
            await asyncio.sleep(2)
        return await self.agents.job(server, job_id)

    @staticmethod
    def _job_result_message(
        target: RemoteServer,
        result: dict,
        job_id: str,
    ) -> str:
        operation = str(result.get("operation", "operation"))
        if result.get("state") == "succeeded":
            message = f"**{target.name}**: `{operation}` completed."
            output = str(result.get("output", "")).strip()
            if operation == "update" and output:
                safe_output = output[-1400:].replace("```", "` ` `")
                message += f"\n```text\n{safe_output}\n```"
            return message
        if result.get("state") == "failed":
            error = str(result.get("error", "Unknown error"))[-1500:]
            return f"**{target.name}**: `{operation}` failed.\n```{error}```"
        return (
            f"**{target.name}**: operation is still running "
            f"(job `{job_id}`). Check the web UI."
        )

    async def setup_hook(self) -> None:
        if self.config.discord_guild_id:
            guild = discord.Object(id=self.config.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            LOG.info("Discord commands synced to guild %s", guild.id)
        else:
            await self.tree.sync()
            LOG.info("Discord commands synced globally")

    async def on_ready(self) -> None:
        LOG.info("Discord connected as %s", self.user)
        if self._desired_presence_level is not None:
            await self._update_health_presence(
                self._desired_presence_level,
                force=True,
            )
        channel_id = self.config.announcement_channel_id
        if not channel_id or self._online_announced:
            return
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if not hasattr(channel, "send"):
                raise TypeError("configured channel does not support messages")
            await channel.send(
                "🟢 **Minecraft Manager is online.** "
                "The Raspberry Pi controller is connected and ready.\n"
                "Did you miss me?",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self._online_announced = True
            LOG.info("Online announcement sent to Discord channel %s", channel_id)
            await self._send_startup_server_status(channel)
        except (discord.DiscordException, TypeError):
            LOG.exception(
                "Could not send the online announcement to Discord channel %s",
                channel_id,
            )

    async def announce(self, message: str) -> None:
        channel_id = self.config.announcement_channel_id
        if not channel_id:
            LOG.info("Skipping Discord announcement because no channel is configured")
            return
        try:
            if not self.is_ready():
                await asyncio.wait_for(self.wait_until_ready(), timeout=30)
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if not hasattr(channel, "send"):
                raise TypeError("configured channel does not support messages")
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (asyncio.TimeoutError, discord.DiscordException, TypeError):
            LOG.exception("Could not send Discord announcement to channel %s", channel_id)

    async def publish_health(self, snapshot: ControllerHealthSnapshot) -> None:
        self._desired_presence_level = snapshot.level
        await self._update_health_presence(snapshot.level)
        if (
            not self.config.ups.enabled
            or not self.config.ups.discord_status_enabled
            or not self.config.ups.discord_status_channel_id
            or snapshot.ups is None
        ):
            return
        await self._upsert_ups_status_card(snapshot)

    async def _update_health_presence(
        self,
        level: HealthLevel,
        *,
        force: bool = False,
    ) -> None:
        if not self.config.health_presence_enabled:
            return
        async with self._presence_lock:
            if force:
                # A fresh Discord gateway connection starts with the
                # constructor's "Checking" presence until this is re-sent.
                self._presence_level = None
            elif self._presence_level == level:
                return
            try:
                if not self.is_ready():
                    await asyncio.wait_for(self.wait_until_ready(), timeout=30)
                statuses = {
                    HealthLevel.ALL_GOOD: discord.Status.online,
                    HealthLevel.CAUTION: discord.Status.idle,
                    HealthLevel.ATTENTION: discord.Status.dnd,
                }
                await self.change_presence(
                    status=statuses[level],
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=f"Minecraft servers • {level.label}",
                    ),
                )
                self._presence_level = level
                LOG.info("Discord health presence changed to %s", level.label)
            except (asyncio.TimeoutError, discord.DiscordException, TypeError):
                LOG.exception("Could not update Discord health presence")

    async def _upsert_ups_status_card(
        self, snapshot: ControllerHealthSnapshot
    ) -> None:
        signature = self._ups_status_signature(snapshot)
        if (
            self._ups_card_signature == signature
            and not self._ups_card_state_dirty
        ):
            return
        channel_id = self.config.ups.discord_status_channel_id
        if channel_id is None:
            return
        content = self._ups_status_content(snapshot)

        async with self._ups_card_lock:
            if self._ups_card_state_dirty:
                self._ups_card_state_dirty = not self._save_ups_card_state()
            if (
                self._ups_card_signature == signature
                and not self._ups_card_state_dirty
            ):
                return
            if self._ups_card_signature == signature:
                return
            forbidden_message_id: int | None = None
            try:
                channel = await self._resolve_message_channel(channel_id)
                if (
                    self._ups_card_channel_id == channel_id
                    and self._ups_card_message_id is not None
                ):
                    try:
                        if hasattr(channel, "get_partial_message"):
                            message = channel.get_partial_message(
                                self._ups_card_message_id
                            )
                        elif hasattr(channel, "fetch_message"):
                            message = await channel.fetch_message(
                                self._ups_card_message_id
                            )
                        else:
                            raise TypeError(
                                "configured channel does not support message edits"
                            )
                        await message.edit(
                            content=content,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.NotFound:
                        self._ups_card_message_id = None
                    except discord.Forbidden:
                        forbidden_message_id = self._ups_card_message_id
                        self._ups_card_message_id = None

                if self._ups_card_message_id is None:
                    message = await channel.send(
                        content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    self._ups_card_channel_id = channel_id
                    self._ups_card_message_id = int(message.id)
                    self._ups_card_state_dirty = not self._save_ups_card_state()

                self._ups_card_signature = signature
            except (
                asyncio.TimeoutError,
                discord.DiscordException,
                TypeError,
                ValueError,
            ):
                if (
                    forbidden_message_id is not None
                    and self._ups_card_message_id is None
                ):
                    self._ups_card_message_id = forbidden_message_id
                LOG.exception("Could not update the Discord UPS status card")

    @staticmethod
    def _ups_status_signature(snapshot: ControllerHealthSnapshot) -> tuple:
        ups = snapshot.ups
        assert ups is not None
        return (
            snapshot.level.value,
            tuple((server.id, server.state) for server in snapshot.servers),
            ups.available,
            ups.status,
            ups.charge_percent,
        )

    @staticmethod
    def _ups_status_content(snapshot: ControllerHealthSnapshot) -> str:
        ups = snapshot.ups
        assert ups is not None
        if ups.available:
            power = ups_power_label(ups)
            raw_status = ups.status.replace("`", "")
            title = (
                "🔋 **Battery Backup Online and Ready**"
                if is_ups_ready(ups)
                else "🔋 **Battery Backup Status**"
            )
        else:
            power = "Unavailable"
            raw_status = "unknown"
            title = "⚠️ **Battery Backup Status**"

        counts = Counter(server.state for server in snapshot.servers)
        preferred_order = (
            "online",
            "busy",
            "offline",
            "unreachable",
            "unknown",
            "error",
        )
        order = preferred_order + tuple(
            sorted(state for state in counts if state not in preferred_order)
        )
        server_summary = ", ".join(
            f"{counts[state]} {state}"
            for state in order
            if counts[state]
        ) or "unknown"
        return (
            f"{title}\n"
            f"Power: **{power}** (`{raw_status}`)\n"
            f"Battery: **{format_charge_percent(ups.charge_percent)}**\n"
            f"Monitoring: {snapshot.level.icon} **{snapshot.level.label}**\n"
            f"Servers: **{server_summary}**\n"
            f"Last change: <t:{int(snapshot.observed_at)}:R>"
        )

    def _load_ups_card_state(self) -> None:
        if self._health_state_file is None:
            return
        try:
            payload = json.loads(self._health_state_file.read_text(encoding="utf-8"))
            channel_id = int(payload["channel_id"])
            message_id = int(payload["message_id"])
            if (
                channel_id == self.config.ups.discord_status_channel_id
                and channel_id > 0
                and message_id > 0
            ):
                self._ups_card_channel_id = channel_id
                self._ups_card_message_id = message_id
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            LOG.exception(
                "Could not load Discord UPS card state from %s",
                self._health_state_file,
            )

    def _save_ups_card_state(self) -> bool:
        if (
            self._health_state_file is None
            or self._ups_card_channel_id is None
            or self._ups_card_message_id is None
        ):
            return self._health_state_file is None
        temporary = self._health_state_file.with_suffix(
            self._health_state_file.suffix + ".tmp"
        )
        try:
            self._health_state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "channel_id": self._ups_card_channel_id,
                        "message_id": self._ups_card_message_id,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._health_state_file)
            return True
        except OSError:
            LOG.exception(
                "Could not save Discord UPS card state to %s",
                self._health_state_file,
            )
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    async def start_player_session(
        self,
        channel_id: int,
        player_name: str,
        server_name: str,
        started_at: float,
    ) -> int | None:
        content = self._player_session_content(
            player_name,
            server_name,
            started_at,
            event="joined",
        )
        return await self._send_player_session(channel_id, content)

    async def update_player_session(
        self,
        channel_id: int,
        message_id: int,
        player_name: str,
        server_name: str,
        started_at: float,
    ) -> int | None:
        content = self._player_session_content(
            player_name,
            server_name,
            started_at,
            event="online",
        )
        return await self._edit_player_session(channel_id, message_id, content)

    async def finish_player_session(
        self,
        channel_id: int,
        message_id: int,
        player_name: str,
        server_name: str,
        started_at: float,
        ended_at: float,
    ) -> int | None:
        content = self._player_session_content(
            player_name,
            server_name,
            started_at,
            event="left",
            ended_at=ended_at,
        )
        return await self._edit_player_session(channel_id, message_id, content)

    async def _send_player_session(self, channel_id: int, content: str) -> int | None:
        try:
            channel = await self._resolve_message_channel(channel_id)
            message = await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return int(message.id)
        except (asyncio.TimeoutError, discord.DiscordException, TypeError, ValueError):
            LOG.exception("Could not send player session message to channel %s", channel_id)
            return None

    async def _edit_player_session(
        self,
        channel_id: int,
        message_id: int,
        content: str,
    ) -> int | None:
        try:
            channel = await self._resolve_message_channel(channel_id)
            if hasattr(channel, "get_partial_message"):
                message = channel.get_partial_message(message_id)
            elif hasattr(channel, "fetch_message"):
                message = await channel.fetch_message(message_id)
            else:
                raise TypeError("configured channel does not support message edits")
            await message.edit(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return message_id
        except discord.NotFound:
            LOG.warning(
                "Player session message %s no longer exists; creating a replacement",
                message_id,
            )
            return await self._send_player_session(channel_id, content)
        except (asyncio.TimeoutError, discord.DiscordException, TypeError):
            LOG.exception("Could not edit player session message %s", message_id)
            return None

    async def _resolve_message_channel(self, channel_id: int):
        if not self.is_ready():
            await asyncio.wait_for(self.wait_until_ready(), timeout=30)
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise TypeError("configured channel does not support messages")
        return channel

    @staticmethod
    def _player_session_content(
        player_name: str,
        server_name: str,
        started_at: float,
        *,
        event: str,
        ended_at: float | None = None,
    ) -> str:
        player = discord.utils.escape_markdown(player_name)
        server = discord.utils.escape_markdown(server_name)
        started = int(started_at)
        if event == "joined":
            return (
                f"🟢 **{player} joined Minecraft**\n"
                f"Current server: **{server}**\n"
                f"Session started: <t:{started}:R>"
            )
        if event == "online":
            return (
                f"🔄 **{player} is playing Minecraft**\n"
                f"Current server: **{server}**\n"
                f"Session started: <t:{started}:R>"
            )
        ended = int(ended_at if ended_at is not None else started_at)
        return (
            f"⚪ **{player} left Minecraft**\n"
            f"Last server: **{server}**\n"
            f"Joined: <t:{started}:f> · Left: <t:{ended}:f>"
        )

    async def _send_startup_server_status(
        self, channel: discord.abc.Messageable
    ) -> None:
        if not self.config.servers:
            return
        message = await self._server_status_message()
        try:
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            LOG.info(
                "Startup status announced for %s server(s)",
                len(self.config.servers),
            )
        except discord.DiscordException:
            LOG.exception("Could not send the startup server status announcement")

    async def _server_status_message(self) -> str:
        if not self.config.servers:
            return "**Minecraft server status**\nNo servers are configured."
        lines = await asyncio.gather(
            *(self._startup_status_line(server) for server in self.config.servers)
        )
        message = "**Minecraft server status**\n" + "\n".join(lines)
        if len(message) > 1900:
            message = message[:1897] + "..."
        return message

    async def _players_message(self) -> str:
        tracked_servers = tuple(
            server for server in self.config.servers if server.track_players
        )
        if not tracked_servers:
            return (
                "**Active Minecraft players**\n"
                "Player Query is not configured for any server."
            )

        snapshots = await asyncio.gather(
            *(self.agents.players(server) for server in tracked_servers),
            return_exceptions=True,
        )
        players: dict[str, tuple[str, list[str]]] = {}
        unavailable: list[str] = []
        for server, snapshot in zip(tracked_servers, snapshots, strict=True):
            if isinstance(snapshot, BaseException):
                unavailable.append(server.name)
                continue
            for raw_name in snapshot:
                player_name = raw_name.strip()
                if not player_name or len(player_name) > 64:
                    continue
                normalized = player_name.casefold()
                if normalized not in players:
                    players[normalized] = (player_name, [])
                locations = players[normalized][1]
                if server.name not in locations:
                    locations.append(server.name)

        lines = ["**Active Minecraft players**"]
        if players:
            for normalized in sorted(players):
                player_name, locations = players[normalized]
                player = discord.utils.escape_markdown(player_name)
                location = " / ".join(
                    discord.utils.escape_markdown(name) for name in locations
                )
                lines.append(f"• **{player}** — {location}")
        else:
            lines.append("No players are currently online.")
        if unavailable:
            server_names = ", ".join(
                discord.utils.escape_markdown(name) for name in unavailable
            )
            lines.append(f"⚠️ Player data unavailable: {server_names}")

        message = "\n".join(lines)
        if len(message) > 1900:
            message = message[:1897] + "..."
        return message

    async def _startup_status_line(self, server: RemoteServer) -> str:
        try:
            result = await self.agents.status(server)
            state = str(result.get("state", "unknown")).lower()
        except AgentUnavailable:
            state = "unreachable"
        except Exception:
            LOG.exception("Unexpected status error for %s during startup", server.id)
            state = "unknown"
        icon = STATUS_ICONS.get(state, STATUS_ICONS["unknown"])
        return f"{icon} **{server.name}** — {state}"

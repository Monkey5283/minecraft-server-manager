from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from .client import AgentClient, AgentUnavailable
from .config import ControllerConfig, RemoteServer


LOG = logging.getLogger("mc_manager.discord")
STATUS_ICONS = {
    "online": "🟢",
    "offline": "🔴",
    "busy": "🟡",
    "unreachable": "⚫",
    "unknown": "⚪",
}


class MinecraftDiscordBot(discord.Client):
    def __init__(self, config: ControllerConfig, agents: AgentClient):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config
        self.agents = agents
        self.servers = {server.id: server for server in config.servers}
        self._online_announced = False
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
                if result["state"] == "succeeded":
                    message = f"**{target.name}**: `{result['operation']}` completed."
                elif result["state"] == "failed":
                    message = (
                        f"**{target.name}**: `{result['operation']}` failed.\n"
                        f"```{str(result.get('error', 'Unknown error'))[-1500:]}```"
                    )
                else:
                    message = (
                        f"**{target.name}**: operation is still running "
                        f"(job `{job['id']}`). Check the web UI."
                    )
                await interaction.followup.send(message, ephemeral=True)
            except AgentUnavailable as exc:
                await interaction.followup.send(str(exc), ephemeral=True)

    async def _wait_for_job(
        self, server: RemoteServer, job_id: str, attempts: int = 120
    ) -> dict:
        for _ in range(attempts):
            job = await self.agents.job(server, job_id)
            if job["state"] in {"succeeded", "failed"}:
                return job
            await asyncio.sleep(2)
        return await self.agents.job(server, job_id)

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

    async def _send_startup_server_status(
        self, channel: discord.abc.Messageable
    ) -> None:
        if not self.config.servers:
            return
        lines = await asyncio.gather(
            *(self._startup_status_line(server) for server in self.config.servers)
        )
        message = "**Minecraft server status**\n" + "\n".join(lines)
        if len(message) > 1900:
            message = message[:1897] + "..."
        try:
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            LOG.info("Startup status announced for %s server(s)", len(lines))
        except discord.DiscordException:
            LOG.exception("Could not send the startup server status announcement")

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

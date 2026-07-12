from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ConfigError(ValueError):
    pass


def _load_toml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Required environment variable is missing: {name}")
    return value


def _validate_id(value: str, label: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise ConfigError(
            f"{label} must contain only lowercase letters, numbers, '-' or '_'"
        )
    return value


def _command_steps(raw: Any, label: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{label} must be a non-empty array of command arrays")
    if all(isinstance(item, str) for item in raw):
        raw = [raw]
    steps: list[tuple[str, ...]] = []
    for index, step in enumerate(raw):
        if (
            not isinstance(step, list)
            or not step
            or not all(isinstance(part, str) and part for part in step)
        ):
            raise ConfigError(f"{label}[{index}] must be a non-empty string array")
        steps.append(tuple(step))
    return tuple(steps)


def _string_array(raw: Any, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{label} must be a non-empty array of strings")
    if not all(isinstance(item, str) and item for item in raw):
        raise ConfigError(f"{label} must be an array of non-empty strings")
    return tuple(raw)


@dataclass(frozen=True)
class PlayerQueryConfig:
    host: str
    port: int
    timeout_seconds: float = 3.0
    offline_status_codes: tuple[int, ...] = (3,)


@dataclass(frozen=True)
class AgentServer:
    id: str
    name: str
    working_directory: Path
    actions: dict[str, tuple[tuple[str, ...], ...]]
    scripts: dict[str, tuple[tuple[str, ...], ...]]
    timeout_seconds: int = 120
    update_timeout_seconds: int = 1800
    player_query: PlayerQueryConfig | None = None


@dataclass(frozen=True)
class AgentConfig:
    name: str
    bind: str
    port: int
    token: str
    servers: tuple[AgentServer, ...]


@dataclass(frozen=True)
class RemoteServer:
    id: str
    name: str
    agent_url: str
    token: str
    track_players: bool = False


@dataclass(frozen=True)
class PlayerTrackingConfig:
    enabled: bool = False
    channel_id: int | None = None
    poll_interval_seconds: float = 5.0
    leave_grace_seconds: float = 10.0
    state_file: Path = Path("/var/lib/minecraft-manager/player-sessions.json")


@dataclass(frozen=True)
class UPSConfig:
    enabled: bool = False
    ups_name: str = "ups"
    status_command: tuple[str, ...] = ("/usr/bin/upsc", "ups", "ups.status")
    charge_command: tuple[str, ...] = ("/usr/bin/upsc", "ups", "battery.charge")
    poll_interval_seconds: int = 15
    on_battery_delay_seconds: int = 30
    stop_timeout_seconds: int = 180
    downstream_shutdown_script: str = "shutdown_host"
    local_shutdown_delay_seconds: int = 15
    local_shutdown_command: tuple[str, ...] = ("/usr/bin/systemctl", "poweroff")


@dataclass(frozen=True)
class ControllerConfig:
    bind: str
    port: int
    web_username: str
    web_password: str
    session_secret: str
    cookie_secure: bool
    discord_token: str
    discord_guild_id: int | None
    announcement_channel_id: int | None
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_role_ids: frozenset[int] = field(default_factory=frozenset)
    servers: tuple[RemoteServer, ...] = ()
    ups: UPSConfig = field(default_factory=UPSConfig)
    player_tracking: PlayerTrackingConfig = field(default_factory=PlayerTrackingConfig)


def load_agent_config(path: str | Path) -> AgentConfig:
    data = _load_toml(path)
    agent = data.get("agent", {})
    token = _required_env(str(agent.get("token_env", "MC_AGENT_TOKEN")))

    servers: list[AgentServer] = []
    seen: set[str] = set()
    for raw in data.get("servers", []):
        server_id = _validate_id(str(raw.get("id", "")), "Agent server id")
        if server_id in seen:
            raise ConfigError(f"Duplicate agent server id: {server_id}")
        seen.add(server_id)
        working_directory = Path(str(raw.get("working_directory", "."))).resolve()
        raw_actions = raw.get("actions", {})
        actions = {
            name: _command_steps(command, f"{server_id}.actions.{name}")
            for name, command in raw_actions.items()
        }
        missing = {"start", "stop", "restart", "status"} - actions.keys()
        if missing:
            raise ConfigError(
                f"{server_id} is missing required actions: {', '.join(sorted(missing))}"
            )
        unsupported = actions.keys() - {"start", "stop", "restart", "status", "update"}
        if unsupported:
            raise ConfigError(
                f"{server_id} has unsupported actions: {', '.join(sorted(unsupported))}"
            )
        scripts = {
            _validate_id(name, f"{server_id} script name"): _command_steps(
                command, f"{server_id}.scripts.{name}"
            )
            for name, command in raw.get("scripts", {}).items()
        }
        raw_player_query = raw.get("player_query")
        player_query = None
        if raw_player_query is not None:
            if not isinstance(raw_player_query, dict):
                raise ConfigError(f"{server_id}.player_query must be a table")
            query_host = str(raw_player_query.get("host", "127.0.0.1")).strip()
            if not query_host:
                raise ConfigError(f"{server_id}.player_query.host must not be empty")
            query_port = int(raw_player_query.get("port", 0))
            if not 1 <= query_port <= 65535:
                raise ConfigError(
                    f"{server_id}.player_query.port must be between 1 and 65535"
                )
            query_timeout = float(raw_player_query.get("timeout_seconds", 3))
            if not 0.1 <= query_timeout <= 30:
                raise ConfigError(
                    f"{server_id}.player_query.timeout_seconds must be between 0.1 and 30"
                )
            raw_offline_codes = raw_player_query.get("offline_status_codes", [3])
            if (
                not isinstance(raw_offline_codes, list)
                or not raw_offline_codes
                or not all(
                    isinstance(code, int) and not isinstance(code, bool) and 1 <= code <= 255
                    for code in raw_offline_codes
                )
            ):
                raise ConfigError(
                    f"{server_id}.player_query.offline_status_codes must be a "
                    "non-empty array of exit codes from 1 to 255"
                )
            player_query = PlayerQueryConfig(
                host=query_host,
                port=query_port,
                timeout_seconds=query_timeout,
                offline_status_codes=tuple(raw_offline_codes),
            )
        servers.append(
            AgentServer(
                id=server_id,
                name=str(raw.get("name", server_id)),
                working_directory=working_directory,
                actions=actions,
                scripts=scripts,
                timeout_seconds=int(raw.get("timeout_seconds", 120)),
                update_timeout_seconds=int(raw.get("update_timeout_seconds", 1800)),
                player_query=player_query,
            )
        )
    if not servers:
        raise ConfigError("At least one [[servers]] entry is required")
    return AgentConfig(
        name=str(agent.get("name", "minecraft-host")),
        bind=str(agent.get("bind", "0.0.0.0")),
        port=int(agent.get("port", 8766)),
        token=token,
        servers=tuple(servers),
    )


def load_controller_config(path: str | Path) -> ControllerConfig:
    data = _load_toml(path)
    controller = data.get("controller", {})
    auth = data.get("auth", {})
    discord = data.get("discord", {})
    ups = data.get("ups", {})
    player_tracking = data.get("player_tracking", {})

    servers: list[RemoteServer] = []
    seen: set[str] = set()
    for raw in data.get("servers", []):
        server_id = _validate_id(str(raw.get("id", "")), "Controller server id")
        if server_id in seen:
            raise ConfigError(f"Duplicate controller server id: {server_id}")
        seen.add(server_id)
        url = str(raw.get("agent_url", "")).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{server_id}.agent_url must begin with http:// or https://")
        servers.append(
            RemoteServer(
                id=server_id,
                name=str(raw.get("name", server_id)),
                agent_url=url,
                token=_required_env(str(raw.get("token_env", "MC_AGENT_TOKEN"))),
                track_players=bool(raw.get("track_players", False)),
            )
        )
    if not servers:
        raise ConfigError("At least one [[servers]] entry is required")

    guild_id = int(discord.get("guild_id", 0)) or None
    announcement_channel_id = int(discord.get("announcement_channel_id", 0)) or None
    player_channel_id = (
        int(player_tracking.get("channel_id", announcement_channel_id or 0)) or None
    )
    player_tracking_enabled = bool(player_tracking.get("enabled", False))
    if player_tracking_enabled and not player_channel_id:
        raise ConfigError(
            "player_tracking.channel_id or discord.announcement_channel_id is required "
            "when player tracking is enabled"
        )
    if player_tracking_enabled and not any(server.track_players for server in servers):
        raise ConfigError(
            "At least one controller [[servers]] entry must set track_players = true "
            "when player tracking is enabled"
        )
    ups_name = str(ups.get("ups_name", "ups"))
    status_command = _string_array(
        ups.get("status_command", ["/usr/bin/upsc", ups_name, "ups.status"]),
        "ups.status_command",
    )
    default_charge_command = (
        (*status_command[:-1], "battery.charge")
        if status_command
        else ("/usr/bin/upsc", ups_name, "battery.charge")
    )
    charge_command = _string_array(
        ups.get("charge_command", list(default_charge_command)),
        "ups.charge_command",
    )
    local_shutdown_command = _string_array(
        ups.get("local_shutdown_command", ["/usr/bin/systemctl", "poweroff"]),
        "ups.local_shutdown_command",
    )
    downstream_shutdown_script = _validate_id(
        str(ups.get("downstream_shutdown_script", "shutdown_host")),
        "ups.downstream_shutdown_script",
    )

    return ControllerConfig(
        bind=str(controller.get("bind", "0.0.0.0")),
        port=int(controller.get("port", 8080)),
        web_username=str(auth.get("web_username", "admin")),
        web_password=_required_env(str(auth.get("web_password_env", "MC_WEB_PASSWORD"))),
        session_secret=_required_env(
            str(auth.get("session_secret_env", "MC_SESSION_SECRET"))
        ),
        cookie_secure=bool(auth.get("cookie_secure", False)),
        discord_token=_required_env(
            str(discord.get("discord_token_env", "DISCORD_BOT_TOKEN"))
        ),
        discord_guild_id=guild_id,
        announcement_channel_id=announcement_channel_id,
        allowed_user_ids=frozenset(int(item) for item in discord.get("allowed_user_ids", [])),
        allowed_role_ids=frozenset(int(item) for item in discord.get("allowed_role_ids", [])),
        servers=tuple(servers),
        ups=UPSConfig(
            enabled=bool(ups.get("enabled", False)),
            ups_name=ups_name,
            status_command=status_command,
            charge_command=charge_command,
            poll_interval_seconds=max(5, int(ups.get("poll_interval_seconds", 15))),
            on_battery_delay_seconds=max(
                0, int(ups.get("on_battery_delay_seconds", 30))
            ),
            stop_timeout_seconds=max(10, int(ups.get("stop_timeout_seconds", 180))),
            downstream_shutdown_script=downstream_shutdown_script,
            local_shutdown_delay_seconds=max(
                0, int(ups.get("local_shutdown_delay_seconds", 15))
            ),
            local_shutdown_command=local_shutdown_command,
        ),
        player_tracking=PlayerTrackingConfig(
            enabled=player_tracking_enabled,
            channel_id=player_channel_id,
            poll_interval_seconds=max(
                2.0, float(player_tracking.get("poll_interval_seconds", 5))
            ),
            leave_grace_seconds=max(
                0.0, float(player_tracking.get("leave_grace_seconds", 10))
            ),
        ),
    )

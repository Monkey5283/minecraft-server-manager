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


@dataclass(frozen=True)
class AgentServer:
    id: str
    name: str
    working_directory: Path
    actions: dict[str, tuple[tuple[str, ...], ...]]
    scripts: dict[str, tuple[tuple[str, ...], ...]]
    timeout_seconds: int = 120
    update_timeout_seconds: int = 1800


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
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_role_ids: frozenset[int] = field(default_factory=frozenset)
    servers: tuple[RemoteServer, ...] = ()


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
        servers.append(
            AgentServer(
                id=server_id,
                name=str(raw.get("name", server_id)),
                working_directory=working_directory,
                actions=actions,
                scripts=scripts,
                timeout_seconds=int(raw.get("timeout_seconds", 120)),
                update_timeout_seconds=int(raw.get("update_timeout_seconds", 1800)),
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
            )
        )
    if not servers:
        raise ConfigError("At least one [[servers]] entry is required")

    guild_id = int(discord.get("guild_id", 0)) or None
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
            str(auth.get("discord_token_env", "DISCORD_BOT_TOKEN"))
        ),
        discord_guild_id=guild_id,
        allowed_user_ids=frozenset(int(item) for item in discord.get("allowed_user_ids", [])),
        allowed_role_ids=frozenset(int(item) for item in discord.get("allowed_role_ids", [])),
        servers=tuple(servers),
    )

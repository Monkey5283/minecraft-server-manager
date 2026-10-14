from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from zoneinfo import available_timezones

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from .client import AgentClient, AgentUnavailable
from .agent_registry import PairedAgent, PairedAgentStore, RegisteredServer
from .config import ConfigError, ControllerConfig, ID_PATTERN, RemoteServer, load_controller_config
from .discovery import DiscoveryRegistry, listen_for_agents
from .discord_bot import MinecraftDiscordBot
from .health import ControllerHealthMonitor
from .panel import PanelStore, NewUser, PasswordChange, RestartSchedule, password_matches
from .automation import ServerAutomation, RecoverySettings
from .folder_transfer import build_folder_archive
from .file_manager import FileManagerError, ManagedDownload
from .player_monitor import PlayerPresenceMonitor
from .seasonal_events import SeasonalEventMonitor
from .server_registry import (
    ManagedServer,
    ManagedServerRegistry,
    ServerRegistryError,
)
from .ups import UPSMonitor


LOG = logging.getLogger("mc_manager.controller")
STATIC_DIR = Path(__file__).parent / "static"
MAX_PROXY_UPLOAD_BYTES = 128 * 1024 * 1024
UPDATE_DIRECTORY = Path("/var/lib/minecraft-manager")
UPDATE_PATH_UNIT = Path("/etc/systemd/system/mc-manager-update.path")
DEFAULT_MANAGED_SERVERS_FILE = Path(
    "/var/lib/minecraft-manager/managed-servers.json"
)


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class FileWriteRequest(BaseModel):
    path: str
    content: str
    expected_version: str | None = None


class DirectoryCreateRequest(BaseModel):
    path: str


class ConsoleCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4096)


class PairAgentRequest(BaseModel):
    agent_id: str
    token: str


class AdoptAgentRequest(BaseModel):
    source_server_id: str


class ProvisionServerRequest(BaseModel):
    id: str
    name: str
    type: str
    version: str
    port: int = 25565
    minimum_memory: str = "1G"
    maximum_memory: str = "4G"
    java_path: str = "/usr/bin/java"
    accept_eula: bool = False


class SoftwareChangeRequest(BaseModel):
    type: str
    version: str
    minimum_memory: str = "1G"
    maximum_memory: str = "4G"
    java_path: str = "/usr/bin/java"
    accept_eula: bool = False
    confirm_backup: bool = False


class DeleteServerRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=64)


class ServerRegistrationRequest(BaseModel):
    server_id: str
    source_server_id: str
    name: str
    track_players: bool = False


class ServerRegistrationUpdate(BaseModel):
    name: str
    track_players: bool = False


class ServerRemovalRequest(BaseModel):
    confirm_id: str


def create_controller_app(
    config: ControllerConfig,
    *,
    managed_servers_file: Path | None = None,
) -> FastAPI:
    base_servers = config.servers
    base_server_map = {server.id: server for server in base_servers}
    registry_path = managed_servers_file or Path(
        os.getenv("MC_MANAGED_SERVERS_FILE", DEFAULT_MANAGED_SERVERS_FILE)
    )
    registry = ManagedServerRegistry(registry_path, base_servers)
    registry.load()
    config = replace(
        config, servers=registry.active_base_servers() + registry.materialized()
    )
    agents = AgentClient()
    discovery = DiscoveryRegistry()
    paired = PairedAgentStore(config.agent_registry_file)
    panel = PanelStore(config.agent_registry_file.with_name("panel-state.json"), config.web_username, config.web_password)
    bot = MinecraftDiscordBot(
        config,
        agents,
        health_state_file=Path(
            "/var/lib/minecraft-manager/ups-status-card.json"
        ),
    )
    servers = {server.id: server for server in config.servers}
    automation = ServerAutomation(panel, agents, servers, bot.announce if hasattr(bot, "announce") else None, ups_enabled=config.ups.enabled)
    agents.operation_observer = automation.observe_operation
    dynamic_owner: dict[str, str] = {}
    registry_lock = asyncio.Lock()
    player_monitor_holder: list[PlayerPresenceMonitor | None] = [None]

    def sync_runtime_servers() -> None:
        runtime_servers = list(
            registry.active_base_servers() + registry.materialized()
        )
        reserved_ids = {server.id for server in runtime_servers}
        dynamic_owner.clear()
        for paired_agent in paired.agents.values():
            for item in paired_agent.servers:
                if item.id in reserved_ids or item.id in dynamic_owner:
                    continue
                runtime_servers.append(
                    RemoteServer(
                        id=item.id,
                        name=item.name,
                        agent_url=paired_agent.url,
                        token=paired_agent.token,
                        track_players=item.track_players,
                    )
                )
                dynamic_owner[item.id] = paired_agent.id
        object.__setattr__(config, "servers", tuple(runtime_servers))
        servers.clear()
        servers.update((server.id, server) for server in runtime_servers)
        if hasattr(bot, "replace_servers"):
            bot.replace_servers(runtime_servers)
        if player_monitor_holder[0] is not None:
            player_monitor_holder[0].set_servers(tuple(runtime_servers))

    def remote_for_agent(agent: PairedAgent) -> RemoteServer:
        return RemoteServer(
            id=agent.id,
            name=agent.name,
            agent_url=agent.url,
            token=agent.token,
        )

    def sync_paired_agent(agent: PairedAgent, info: dict | None = None) -> None:
        if info is not None:
            raw_servers = info.get("servers", [])
            if not isinstance(raw_servers, list):
                raise HTTPException(status_code=502, detail="Agent returned invalid servers")
            refreshed: list[RegisteredServer] = []
            for raw in raw_servers:
                server_id = str(raw.get("id", "")) if isinstance(raw, dict) else ""
                if not ID_PATTERN.fullmatch(server_id):
                    raise HTTPException(
                        status_code=502, detail="Agent returned an invalid server id"
                    )
                owner = dynamic_owner.get(server_id)
                if server_id in servers and owner not in {None, agent.id}:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Server id is already used: {server_id}",
                    )
                if server_id in servers and owner is None:
                    configured = base_server_map.get(server_id)
                    same_trusted_agent = configured is not None and (
                        configured.agent_url.rstrip("/") == agent.url.rstrip("/")
                        and hmac.compare_digest(configured.token, agent.token)
                    )
                    if not same_trusted_agent:
                        raise HTTPException(
                            status_code=409,
                            detail=f"Server id conflicts with controller.toml: {server_id}",
                        )
                refreshed.append(
                    RegisteredServer(
                        id=server_id,
                        name=str(raw.get("name", server_id)),
                        track_players=bool(raw.get("track_players", False)),
                    )
                )
            agent.servers = refreshed

        sync_runtime_servers()

    sync_runtime_servers()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bot_task = asyncio.create_task(bot.start(config.discord_token))
        health_enabled = config.health_presence_enabled or (
            config.ups.enabled
            and config.ups.discord_status_enabled
            and config.ups.discord_status_channel_id is not None
        )
        health_monitor = (
            ControllerHealthMonitor(config, agents, bot)
            if health_enabled
            else None
        )

        async def supervise_health(monitor: ControllerHealthMonitor) -> None:
            while True:
                try:
                    await monitor.run()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Health monitor stopped unexpectedly; retrying")
                else:
                    LOG.error("Health monitor ended unexpectedly; retrying")
                await asyncio.sleep(5)

        health_task = (
            asyncio.create_task(supervise_health(health_monitor))
            if health_monitor is not None
            else None
        )

        async def supervise_ups() -> None:
            def power_status(reading):
                automation.power_reading(reading)
                if health_monitor is not None:
                    health_monitor.update_ups(reading)

            while True:
                monitor = UPSMonitor(
                    config,
                    agents,
                    bot.announce,
                    status_sink=power_status,
                )
                try:
                    await monitor.run()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("UPS monitor stopped unexpectedly; retrying")
                else:
                    LOG.warning("UPS monitor ended while controller is running; retrying")
                await asyncio.sleep(5)

        ups_task = (
            asyncio.create_task(supervise_ups())
            if config.ups.enabled
            else None
        )
        player_monitor = (
            PlayerPresenceMonitor(config, agents, bot)
            if config.player_tracking.enabled
            else None
        )
        player_monitor_holder[0] = player_monitor
        player_task = (
            asyncio.create_task(player_monitor.run())
            if player_monitor is not None
            else None
        )
        discovery_task = (
            asyncio.create_task(listen_for_agents(discovery, config.discovery_port))
            if config.discovery_enabled
            else None
        )
        app.state.bot_task = bot_task
        app.state.health_task = health_task
        app.state.ups_task = ups_task
        app.state.player_task = player_task
        app.state.player_monitor = player_monitor
        app.state.discovery_task = discovery_task
        seasonal_task = (
            asyncio.create_task(SeasonalEventMonitor(
                config, agents, bot, config.agent_registry_file.with_name("seasonal-announcements.json")
            ).run()) if config.seasonal_announcements_enabled else None
        )
        app.state.seasonal_task = seasonal_task
        schedule_task = asyncio.create_task(automation.run())
        try:
            yield
        finally:
            background_tasks = tuple(
                task
                for task in (health_task, ups_task, player_task, discovery_task, schedule_task, seasonal_task)
                if task is not None
            )
            for task in background_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
            await bot.close()
            if not bot_task.done():
                bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
            await agents.close()

    app = FastAPI(
        title="Minecraft Manager",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret,
        same_site="strict",
        https_only=config.cookie_secure,
        max_age=12 * 60 * 60,
    )

    @app.middleware("http")
    async def prevent_stale_dashboard_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.state.discovery = discovery
    app.state.paired_agents = paired
    app.state.panel = panel
    app.state.automation = automation

    def require_login(request: Request) -> None:
        panel.require(request)

    def find_server(server_id: str) -> RemoteServer:
        server = servers.get(server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Unknown server")
        return server

    def registry_error(exc: ServerRegistryError) -> HTTPException:
        error_status = (
            status.HTTP_500_INTERNAL_SERVER_ERROR
            if str(exc).startswith("Could not save")
            else status.HTTP_400_BAD_REQUEST
        )
        return HTTPException(status_code=error_status, detail=str(exc))

    def unique_agent_sources() -> tuple[RemoteServer, ...]:
        unique: dict[tuple[str, str], RemoteServer] = {}
        for server in base_servers:
            unique.setdefault((server.agent_url, server.token), server)
        return tuple(unique.values())

    async def confirm_agent_server(
        source_server_id: str, server_id: str
    ) -> tuple[RemoteServer, dict]:
        source = base_server_map.get(source_server_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Unknown credential source")
        try:
            entries = await agents.statuses(source)
        except AgentUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        entry = entries.get(server_id)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail="That server is not configured on the selected agent",
            )
        return source, entry

    def agent_file_error(exc: AgentUnavailable) -> HTTPException:
        allowed_statuses = {400, 403, 404, 409, 413, 415}
        response_status = (
            exc.status_code if exc.status_code in allowed_statuses else 502
        )
        return HTTPException(status_code=response_status, detail=str(exc))

    @app.get("/api/session")
    async def session(request: Request) -> dict:
        user = panel.user(request.session)
        return {
            "authenticated": user is not None,
            "username": request.session.get("username") if user else None,
            "admin": bool(user and user["admin"]),
        }

    @app.post("/api/login")
    async def login(payload: LoginRequest, request: Request) -> dict:
        user = panel.data["users"].get(payload.username)
        # Perform a password hash even for unknown usernames.
        encoded = (user or next(iter(panel.data["users"].values())))["hash"]
        valid_password = await asyncio.to_thread(password_matches, payload.password, encoded)
        if not user or not valid_password:
            raise HTTPException(status_code=401, detail="Invalid username or password")
        request.session.clear()
        request.session["authenticated"] = True
        request.session["username"] = payload.username
        request.session["version"] = user["version"]
        return {"ok": True}

    @app.get("/api/users")
    async def list_users(request: Request) -> list[dict]:
        panel.require(request, admin=True)
        return [{"username": name, "admin": value["admin"]} for name, value in panel.data["users"].items()]

    @app.post("/api/users", status_code=201)
    async def add_user(payload: NewUser, request: Request) -> dict:
        panel.require(request, admin=True)
        if payload.username in panel.data["users"]:
            raise HTTPException(409, "Username already exists")
        panel.put("users", payload.username, panel.account(payload.password, payload.admin))
        return {"ok": True}

    @app.put("/api/users/{username}/password")
    async def change_password(username: str, payload: PasswordChange, request: Request) -> dict:
        actor = panel.require(request)
        own = username == request.session["username"]
        if not own and not actor["admin"]:
            raise HTTPException(403, "Administrator access required")
        user = panel.data["users"].get(username)
        if user is None:
            raise HTTPException(404, "Unknown user")
        if own and not password_matches(payload.current_password, user["hash"]):
            raise HTTPException(400, "Current password is incorrect")
        replacement = panel.account(payload.password, user["admin"])
        panel.put("users", username, replacement)
        if own:
            request.session["version"] = replacement["version"]
        return {"ok": True}

    @app.get("/api/servers/{server_id}/restart-schedule")
    async def get_schedule(server_id: str, request: Request) -> dict:
        require_login(request)
        find_server(server_id)
        raw = panel.data["schedules"].get(server_id, {})
        return {**RestartSchedule.model_validate(raw).model_dump(), **raw}

    @app.get("/api/timezones")
    async def timezones(request: Request) -> list[str]:
        require_login(request)
        return sorted(available_timezones())

    @app.get("/api/servers/{server_id}/monitoring")
    async def monitoring(server_id: str, request: Request) -> dict:
        require_login(request)
        find_server(server_id)
        return {"settings": automation.settings(server_id).model_dump(),
                "status": automation.state(server_id), "power_paused": automation.blocked(),
                "discord_configured": config.announcement_channel_id is not None}

    @app.put("/api/servers/{server_id}/monitoring")
    async def save_monitoring(server_id: str, payload: RecoverySettings, request: Request) -> dict:
        require_login(request)
        find_server(server_id)
        panel.put("monitoring", server_id, payload.model_dump())
        automation.next_check.pop(server_id, None)
        return payload.model_dump()

    @app.post("/api/servers/{server_id}/scheduled-restart", status_code=202)
    async def run_scheduled_restart(server_id: str, request: Request) -> dict:
        require_login(request)
        find_server(server_id)
        schedule = RestartSchedule.model_validate(panel.data["schedules"].get(server_id, {}))
        if not schedule.enabled:
            raise HTTPException(409, "Enable and save a restart schedule first")
        try:
            automation.allowed(server_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not automation.launch(server_id, automation.clock() + schedule.warning_seconds, schedule):
            raise HTTPException(409, "A restart script is already running")
        return {"state": "queued"}

    @app.put("/api/servers/{server_id}/restart-schedule")
    async def save_schedule(server_id: str, payload: RestartSchedule, request: Request) -> dict:
        require_login(request)
        find_server(server_id)
        for warning_server in payload.warning_servers:
            find_server(warning_server)
        if payload.enabled:
            # Proxies do not implement Minecraft's say command: choose gameplay servers.
            targets = payload.warning_servers or [server_id]
            for target in targets:
                if "velocity" in target.lower() or "proxy" in target.lower():
                    raise HTTPException(400, "For a proxy restart, select gameplay servers as warning destinations")
                try:
                    status = await agents.status(find_server(target))
                except AgentUnavailable as exc:
                    raise HTTPException(409, "Cannot verify warning destination: " + str(exc)) from exc
                if status.get("console_enabled") is not True:
                    raise HTTPException(409, "Player warnings require console access on " + find_server(target).name)
        task = automation.tasks.get(server_id)
        if task and (automation.state(server_id).get("pending") or {}).get("stage") == "warning":
            task.cancel()
            automation.record(server_id, pending=None, last_result="Countdown canceled after schedule edit")
        previous = panel.data["schedules"].get(server_id, {})
        value = {**payload.model_dump(), **{key: previous[key] for key in ("last_slot", "last_result") if key in previous}}
        panel.put("schedules", server_id, value)
        return value

    update_directory = UPDATE_DIRECTORY

    @app.get("/api/manager/update")
    async def update_status(request: Request) -> dict:
        panel.require(request, admin=True)
        enabled = UPDATE_PATH_UNIT.exists()
        try:
            result = json.loads((update_directory / "manager-update-status.json").read_text())
        except FileNotFoundError:
            result = {"state": "idle"}
        if (update_directory / "manager-update.request").exists():
            result = {"state": "running"}
        return {**result, "enabled": enabled}

    @app.post("/api/manager/update", status_code=202)
    async def update_manager(request: Request) -> dict:
        panel.require(request, admin=True)
        if not UPDATE_PATH_UNIT.exists():
            raise HTTPException(503, "Install the manager update service on the controller first")
        try:
            with (update_directory / "manager-update.request").open("x") as handle:
                handle.write("update\n")
        except FileExistsError:
            raise HTTPException(409, "A manager update is already queued or running")
        return {"state": "queued"}

    @app.post("/api/logout")
    async def logout(request: Request) -> dict:
        request.session.clear()
        return {"ok": True}

    @app.get("/api/server-registry")
    async def server_registry(request: Request) -> dict:
        require_login(request)
        managed_ids = set(registry.entries)
        return {
            "configured": [
                {
                    "id": server.id,
                    "name": server.name,
                    "track_players": server.track_players,
                    "managed": server.id in managed_ids,
                    "source_server_id": (
                        registry.entries[server.id].source_server_id
                        if server.id in managed_ids
                        else server.id
                    ),
                }
                for server in base_servers + registry.materialized()
            ]
        }

    @app.get("/api/server-registry/discover")
    async def discover_servers(request:…6666 tokens truncated…arse_args()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    try:
        config = load_controller_config(Path(args.config))
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    uvicorn.run(create_controller_app(config), host=config.bind, port=config.port)


if __name__ == "__main__":
    main()


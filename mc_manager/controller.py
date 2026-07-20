from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import quote

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
from .player_monitor import PlayerPresenceMonitor
from .server_registry import (
    ManagedServer,
    ManagedServerRegistry,
    ServerRegistryError,
)
from .ups import UPSMonitor


LOG = logging.getLogger("mc_manager.controller")
STATIC_DIR = Path(__file__).parent / "static"
MAX_PROXY_UPLOAD_BYTES = 128 * 1024 * 1024
DEFAULT_MANAGED_SERVERS_FILE = Path(
    "/var/lib/minecraft-manager/managed-servers.json"
)


class LoginRequest(BaseModel):
    username: str
    password: str


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
    config = replace(config, servers=base_servers + registry.materialized())
    agents = AgentClient()
    discovery = DiscoveryRegistry()
    paired = PairedAgentStore(config.agent_registry_file)
    bot = MinecraftDiscordBot(
        config,
        agents,
        health_state_file=Path(
            "/var/lib/minecraft-manager/ups-status-card.json"
        ),
    )
    servers = {server.id: server for server in config.servers}
    dynamic_owner: dict[str, str] = {}
    registry_lock = asyncio.Lock()
    player_monitor_holder: list[PlayerPresenceMonitor | None] = [None]

    def sync_runtime_servers() -> None:
        runtime_servers = list(base_servers + registry.materialized())
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
            while True:
                monitor = UPSMonitor(
                    config,
                    agents,
                    bot.announce,
                    status_sink=(
                        health_monitor.update_ups
                        if health_monitor is not None
                        else None
                    ),
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
        try:
            yield
        finally:
            background_tasks = tuple(
                task
                for task in (health_task, ups_task, player_task, discovery_task)
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

    def require_login(request: Request) -> None:
        if request.session.get("authenticated") is not True:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required"
            )

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
        return {
            "authenticated": request.session.get("authenticated") is True,
            "username": config.web_username
            if request.session.get("authenticated") is True
            else None,
        }

    @app.post("/api/login")
    async def login(payload: LoginRequest, request: Request) -> dict:
        valid_user = hmac.compare_digest(payload.username, config.web_username)
        valid_password = hmac.compare_digest(payload.password, config.web_password)
        if not (valid_user and valid_password):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        request.session.clear()
        request.session["authenticated"] = True
        return {"ok": True}

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
    async def discover_servers(request: Request) -> dict:
        require_login(request)
        sources = unique_agent_sources()
        results = await asyncio.gather(
            *(agents.statuses(source) for source in sources),
            return_exceptions=True,
        )
        candidates: list[dict] = []
        unavailable: list[dict] = []
        registered_ids = set(servers)
        discovered_ids: set[str] = set()
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                unavailable.append(
                    {"source_server_id": source.id, "detail": str(result)}
                )
                continue
            for server_id, entry in result.items():
                if server_id in registered_ids or server_id in discovered_ids:
                    continue
                discovered_ids.add(server_id)
                candidates.append(
                    {
                        "id": server_id,
                        "name": str(entry.get("name") or server_id),
                        "state": str(entry.get("state") or "unknown"),
                        "files_enabled": bool(entry.get("files_enabled", False)),
                        "player_tracking_available": bool(
                            entry.get("player_tracking_available", False)
                        ),
                        "source_server_id": source.id,
                        "source_name": source.name,
                    }
                )
        candidates.sort(key=lambda item: (item["name"].casefold(), item["id"]))
        return {"candidates": candidates, "unavailable": unavailable}

    @app.post("/api/server-registry")
    async def register_server(
        payload: ServerRegistrationRequest, request: Request
    ) -> dict:
        require_login(request)
        async with registry_lock:
            if payload.server_id in servers:
                raise HTTPException(status_code=409, detail="Server is already registered")
            _source, agent_entry = await confirm_agent_server(
                payload.source_server_id, payload.server_id
            )
            if payload.track_players and not agent_entry.get(
                "player_tracking_available", False
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Player tracking is not configured on that agent server",
                )
            try:
                server = registry.add(
                    ManagedServer(
                        id=payload.server_id,
                        name=payload.name,
                        source_server_id=payload.source_server_id,
                        track_players=payload.track_players,
                    )
                )
            except ServerRegistryError as exc:
                raise registry_error(exc) from exc
            sync_runtime_servers()
        return {"ok": True, "id": server.id, "name": server.name}

    @app.put("/api/server-registry/{server_id}")
    async def update_registered_server(
        server_id: str, payload: ServerRegistrationUpdate, request: Request
    ) -> dict:
        require_login(request)
        async with registry_lock:
            current = registry.entries.get(server_id)
            if current is None:
                raise HTTPException(
                    status_code=409,
                    detail="Servers from controller.toml cannot be edited here",
                )
            if payload.track_players and not current.track_players:
                _source, agent_entry = await confirm_agent_server(
                    current.source_server_id, server_id
                )
                if not agent_entry.get("player_tracking_available", False):
                    raise HTTPException(
                        status_code=400,
                        detail="Player tracking is not configured on that agent server",
                    )
            try:
                server = registry.update(
                    server_id,
                    name=payload.name,
                    track_players=payload.track_players,
                )
            except ServerRegistryError as exc:
                raise registry_error(exc) from exc
            sync_runtime_servers()
        return {"ok": True, "id": server.id, "name": server.name}

    @app.post("/api/server-registry/{server_id}/remove")
    async def remove_registered_server(
        server_id: str, payload: ServerRemovalRequest, request: Request
    ) -> dict:
        require_login(request)
        if not hmac.compare_digest(payload.confirm_id, server_id):
            raise HTTPException(status_code=400, detail="Server id confirmation did not match")
        async with registry_lock:
            try:
                registry.remove(server_id)
            except ServerRegistryError as exc:
                raise registry_error(exc) from exc
            sync_runtime_servers()
        return {"ok": True, "id": server_id}

    @app.get("/api/agents/discovered")
    async def discovered_agents(request: Request) -> list[dict]:
        require_login(request)
        return [
            {
                **agent.as_public_dict(),
                "paired": agent.id in paired.agents,
            }
            for agent in discovery.list()
        ]

    @app.get("/api/agents")
    async def paired_agents(request: Request) -> list[dict]:
        require_login(request)
        return [
            {
                "id": agent.id,
                "name": agent.name,
                "url": agent.url,
                "servers": [
                    {
                        "id": server.id,
                        "name": server.name,
                        "track_players": server.track_players,
                    }
                    for server in agent.servers
                ],
            }
            for agent in paired.agents.values()
        ]

    @app.post("/api/agents/pair")
    async def pair_agent(payload: PairAgentRequest, request: Request) -> dict:
        require_login(request)
        discovered = discovery.get(payload.agent_id)
        if discovered is None:
            raise HTTPException(status_code=404, detail="Agent is no longer visible on the LAN")
        candidate = PairedAgent(
            id=payload.agent_id,
            name=discovered.name,
            url=discovered.url,
            token=payload.token.strip(),
        )
        if not candidate.token:
            raise HTTPException(status_code=400, detail="Agent token is required")
        try:
            info = await agents.info(remote_for_agent(candidate))
        except AgentUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if str(info.get("id")) != candidate.id:
            raise HTTPException(status_code=409, detail="Agent identity changed during pairing")
        candidate.name = str(info.get("name", candidate.name))
        sync_paired_agent(candidate, info)
        paired.put(candidate)
        sync_runtime_servers()
        return {"ok": True, "id": candidate.id, "name": candidate.name}

    def find_agent(agent_id: str) -> PairedAgent:
        agent = paired.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Unknown or unpaired agent")
        return agent

    @app.get("/api/agents/{agent_id}/catalog/{server_type}")
    async def agent_catalog(agent_id: str, server_type: str, request: Request) -> dict:
        require_login(request)
        try:
            return await agents.catalog(remote_for_agent(find_agent(agent_id)), server_type)
        except AgentUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/agents/{agent_id}/servers")
    async def provision_server(
        agent_id: str,
        payload: ProvisionServerRequest,
        request: Request,
    ) -> dict:
        require_login(request)
        if payload.id in servers:
            raise HTTPException(status_code=409, detail="Server id is already configured")
        try:
            return await agents.provision(
                remote_for_agent(find_agent(agent_id)), payload.model_dump()
            )
        except AgentUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/agents/{agent_id}/jobs/{job_id}")
    async def provisioning_job(
        agent_id: str,
        job_id: str,
        request: Request,
    ) -> dict:
        require_login(request)
        agent = find_agent(agent_id)
        try:
            result = await agents.job(remote_for_agent(agent), job_id)
            if result.get("state") == "succeeded":
                info = await agents.info(remote_for_agent(agent))
                sync_paired_agent(agent, info)
                paired.put(agent)
            return result
        except AgentUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/servers")
    async def list_servers(request: Request) -> list[dict]:
        require_login(request)

        async def get_status(server: RemoteServer) -> dict:
            try:
                result = await agents.status(server)
                result["controller_id"] = server.id
                result["name"] = server.name
                result["managed_registration"] = server.id in registry.entries
                return result
            except AgentUnavailable as exc:
                return {
                    "id": server.id,
                    "controller_id": server.id,
                    "name": server.name,
                    "state": "unreachable",
                    "detail": str(exc),
                    "actions": [],
                    "scripts": [],
                    "managed_registration": server.id in registry.entries,
                }

        return list(await asyncio.gather(*(get_status(item) for item in servers.values())))

    @app.post("/api/servers/{server_id}/actions/{action}")
    async def action(server_id: str, action: str, request: Request) -> dict:
        require_login(request)
        if action not in {"start", "stop", "restart", "update"}:
            raise HTTPException(status_code=404, detail="Unsupported action")
        try:
            return await agents.action(find_server(server_id), action)
        except AgentUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/servers/{server_id}/scripts/{script_name}")
    async def script(server_id: str, script_name: str, request: Request) -> dict:
        require_login(request)
        try:
            return await agents.script(find_server(server_id), script_name)
        except AgentUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/servers/{server_id}/files")
    async def files(server_id: str, request: Request, path: str = "") -> dict:
        require_login(request)
        try:
            return await agents.files(find_server(server_id), path)
        except AgentUnavailable as exc:
            raise agent_file_error(exc) from exc

    @app.get("/api/servers/{server_id}/files/content")
    async def file_content(server_id: str, path: str, request: Request) -> dict:
        require_login(request)
        try:
            return await agents.file_content(find_server(server_id), path)
        except AgentUnavailable as exc:
            raise agent_file_error(exc) from exc

    @app.get("/api/servers/{server_id}/files/download")
    async def download_file(
        server_id: str,
        path: str,
        request: Request,
    ) -> StreamingResponse:
        require_login(request)
        try:
            response = await agents.download_file(find_server(server_id), path)
        except AgentUnavailable as exc:
            raise agent_file_error(exc) from exc

        download_name = PurePosixPath(path).name or "download"
        headers = {
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(download_name, safe='')}"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        content_length = response.headers.get("content-length")
        if content_length and content_length.isdecimal():
            headers["Content-Length"] = content_length
        return StreamingResponse(
            response.aiter_raw(),
            media_type="application/octet-stream",
            headers=headers,
            background=BackgroundTask(response.aclose),
        )

    @app.put("/api/servers/{server_id}/files/content")
    async def save_file(
        server_id: str, payload: FileWriteRequest, request: Request
    ) -> dict:
        require_login(request)
        try:
            return await agents.save_file(
                find_server(server_id),
                payload.path,
                payload.content,
                payload.expected_version,
            )
        except AgentUnavailable as exc:
            raise agent_file_error(exc) from exc

    @app.post("/api/servers/{server_id}/files/directory")
    async def create_directory(
        server_id: str, payload: DirectoryCreateRequest, request: Request
    ) -> dict:
        require_login(request)
        try:
            return await agents.create_directory(find_server(server_id), payload.path)
        except AgentUnavailable as exc:
            raise agent_file_error(exc) from exc

    @app.delete("/api/servers/{server_id}/files")
    async def delete_file(server_id: str, path: str, request: Request) -> dict:
        require_login(request)
        try:
            result = await agents.delete_file(find_server(server_id), path)
        except AgentUnavailable as exc:
            raise agent_file_error(exc) from exc
        LOG.warning(
            "Dashboard user=%s deleted server path server=%s path=%s",
            config.web_username,
            server_id,
            result.get("path", path),
        )
        return result

    @app.get("/api/servers/{server_id}/console")
    async def console_output(server_id: str, cursor: int, request: Request) -> dict:
        require_login(request)
        try:
            return await agents.console_output(find_server(server_id), cursor)
        except AgentUnavailable as exc:
            raise agent_file_error(exc) from exc

    @app.post("/api/servers/{server_id}/console")
    async def console_command(
        server_id: str, payload: ConsoleCommandRequest, request: Request
    ) -> dict:
        require_login(request)
        try:
            result = await agents.console_command(
                find_server(server_id), payload.command
            )
        except AgentUnavailable as exc:
            raise agent_file_error(exc) from exc
        normalized = payload.command.lstrip().lstrip("/").lstrip()
        verb = normalized.split(maxsplit=1)[0] if normalized else "invalid"
        fingerprint = hashlib.sha256(payload.command.encode("utf-8")).hexdigest()[:12]
        LOG.warning(
            "Dashboard Minecraft console command user=%s server=%s verb=%s fingerprint=%s",
            config.web_username,
            server_id,
            verb,
            fingerprint,
        )
        return result

    @app.put("/api/servers/{server_id}/files/upload")
    async def upload_file(
        server_id: str,
        request: Request,
        path: str,
        overwrite: bool = False,
    ) -> dict:
        require_login(request)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
            if declared_size > MAX_PROXY_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Upload exceeds proxy limit")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_PROXY_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Upload exceeds proxy limit")
        try:
            return await agents.upload_file(
                find_server(server_id),
                path,
                bytes(content),
                overwrite=overwrite,
            )
        except AgentUnavailable as exc:
            raise agent_file_error(exc) from exc

    @app.get("/api/servers/{server_id}/jobs/{job_id}")
    async def job(server_id: str, job_id: str, request: Request) -> dict:
        require_login(request)
        try:
            return await agents.job(find_server(server_id), job_id)
        except AgentUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Minecraft manager controller")
    parser.add_argument(
        "--config",
        default=os.getenv("MC_CONTROLLER_CONFIG", "config/controller.toml"),
        help="Path to controller TOML configuration",
    )
    return parser.parse_args()


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

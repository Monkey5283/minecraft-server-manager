from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .client import AgentClient, AgentUnavailable
from .config import ConfigError, ControllerConfig, RemoteServer, load_controller_config
from .discord_bot import MinecraftDiscordBot
from .health import ControllerHealthMonitor
from .player_monitor import PlayerPresenceMonitor
from .ups import UPSMonitor


LOG = logging.getLogger("mc_manager.controller")
STATIC_DIR = Path(__file__).parent / "static"
MAX_PROXY_UPLOAD_BYTES = 128 * 1024 * 1024


class LoginRequest(BaseModel):
    username: str
    password: str


class FileWriteRequest(BaseModel):
    path: str
    content: str
    expected_version: str | None = None


class DirectoryCreateRequest(BaseModel):
    path: str


def create_controller_app(config: ControllerConfig) -> FastAPI:
    agents = AgentClient()
    bot = MinecraftDiscordBot(
        config,
        agents,
        health_state_file=Path(
            "/var/lib/minecraft-manager/ups-status-card.json"
        ),
    )
    servers = {server.id: server for server in config.servers}

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
        player_task = (
            asyncio.create_task(player_monitor.run())
            if player_monitor is not None
            else None
        )
        app.state.bot_task = bot_task
        app.state.health_task = health_task
        app.state.ups_task = ups_task
        app.state.player_task = player_task
        try:
            yield
        finally:
            background_tasks = tuple(
                task
                for task in (health_task, ups_task, player_task)
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

    @app.get("/api/servers")
    async def list_servers(request: Request) -> list[dict]:
        require_login(request)

        async def get_status(server: RemoteServer) -> dict:
            try:
                result = await agents.status(server)
                result["controller_id"] = server.id
                result["name"] = server.name
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
                }

        return list(await asyncio.gather(*(get_status(item) for item in config.servers)))

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

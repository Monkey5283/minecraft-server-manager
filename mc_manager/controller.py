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
from .ups import UPSMonitor


LOG = logging.getLogger("mc_manager.controller")
STATIC_DIR = Path(__file__).parent / "static"


class LoginRequest(BaseModel):
    username: str
    password: str


def create_controller_app(config: ControllerConfig) -> FastAPI:
    agents = AgentClient()
    bot = MinecraftDiscordBot(config, agents)
    servers = {server.id: server for server in config.servers}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bot_task = asyncio.create_task(bot.start(config.discord_token))
        ups_monitor = UPSMonitor(config, agents, bot.announce)
        ups_task = (
            asyncio.create_task(ups_monitor.run())
            if config.ups.enabled
            else None
        )
        app.state.bot_task = bot_task
        app.state.ups_task = ups_task
        try:
            yield
        finally:
            if ups_task and not ups_task.done():
                ups_task.cancel()
            await bot.close()
            if not bot_task.done():
                bot_task.cancel()
            await asyncio.gather(
                *(task for task in (bot_task, ups_task) if task),
                return_exceptions=True,
            )
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

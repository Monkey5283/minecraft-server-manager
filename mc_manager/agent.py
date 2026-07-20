from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .config import AgentConfig, AgentServer, ConfigError, load_agent_config
from .discovery import advertise_agent
from .file_manager import FileManagerError, ServerFileManager
from .minecraft_query import MinecraftQueryError, query_players
from .server_catalog import CatalogError, list_versions


LOG = logging.getLogger("mc_manager.agent")
MAX_OUTPUT = 32_000
MAX_JOBS = 200


class FileWriteRequest(BaseModel):
    path: str
    content: str
    expected_version: str | None = None


class DirectoryCreateRequest(BaseModel):
    path: str


class ProvisionRequest(BaseModel):
    id: str
    name: str
    type: str
    version: str
    port: int = 25565
    minimum_memory: str = "1G"
    maximum_memory: str = "4G"
    java_path: str = "/usr/bin/java"
    accept_eula: bool = False


class CommandFailed(RuntimeError):
    def __init__(self, executable: str, returncode: int, output: str):
        self.executable = executable
        self.returncode = returncode
        self.output = output
        super().__init__(
            f"Command {executable} exited with code {returncode}\n{output}"
        )


@dataclass
class Job:
    id: str
    server_id: str
    operation: str
    state: str = "queued"
    started_at: float | None = None
    completed_at: float | None = None
    output: str = ""
    error: str | None = None


class AgentRuntime:
    def __init__(self, config: AgentConfig, config_path: Path | None = None):
        self.config = config
        self.config_path = config_path
        self.provisioning_lock = asyncio.Lock()
        self.servers = {server.id: server for server in config.servers}
        self.locks = {server.id: asyncio.Lock() for server in config.servers}
        self.file_managers = {
            server.id: ServerFileManager(server.file_manager)
            for server in config.servers
            if server.file_manager is not None
        }
        self.jobs: OrderedDict[str, Job] = OrderedDict()

    def replace_config(self, config: AgentConfig) -> None:
        self.config = config
        self.servers = {server.id: server for server in config.servers}
        self.locks = {
            server.id: self.locks.get(server.id, asyncio.Lock())
            for server in config.servers
        }
        self.file_managers = {
            server.id: ServerFileManager(server.file_manager)
            for server in config.servers
            if server.file_manager is not None
        }

    async def execute_steps(
        self,
        server: AgentServer,
        steps: tuple[tuple[str, ...], ...],
        timeout: int,
    ) -> str:
        chunks: list[str] = []
        for command in steps:
            LOG.info("Running %s for %s", command[0], server.id)
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=server.working_directory,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=os.environ.copy(),
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                raise RuntimeError(f"Could not start {command[0]}: {exc}") from exc
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise RuntimeError(f"Command timed out after {timeout} seconds") from exc
            text = stdout.decode("utf-8", errors="replace")
            chunks.append(f"$ {command[0]}\n{text}".strip())
            if process.returncode != 0:
                raise CommandFailed(command[0], process.returncode, text)
        return "\n\n".join(chunks)[-MAX_OUTPUT:]

    async def run_job(
        self,
        job: Job,
        server: AgentServer,
        steps: tuple[tuple[str, ...], ...],
        timeout: int,
    ) -> None:
        async with self.locks[server.id]:
            job.state = "running"
            job.started_at = time.time()
            try:
                job.output = await self.execute_steps(server, steps, timeout)
                job.state = "succeeded"
            except Exception as exc:
                LOG.exception("Job %s failed", job.id)
                job.error = str(exc)[-MAX_OUTPUT:]
                job.state = "failed"
            finally:
                job.completed_at = time.time()

    def start_job(
        self,
        server: AgentServer,
        operation: str,
        steps: tuple[tuple[str, ...], ...],
        timeout: int,
    ) -> Job:
        while len(self.jobs) >= MAX_JOBS:
            self.jobs.popitem(last=False)
        job = Job(id=str(uuid.uuid4()), server_id=server.id, operation=operation)
        self.jobs[job.id] = job
        asyncio.create_task(self.run_job(job, server, steps, timeout))
        return job

    async def run_provision_job(self, job: Job, payload: dict) -> None:
        async with self.provisioning_lock:
            job.state = "running"
            job.started_at = time.time()
            command = (
                "sudo",
                "-n",
                "/opt/minecraft-manager/venv/bin/mc-manager-provision",
                "--request",
                json.dumps(payload, separators=(",", ":")),
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=os.environ.copy(),
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=3600)
                output = stdout.decode("utf-8", errors="replace")[-MAX_OUTPUT:]
                if process.returncode != 0:
                    raise CommandFailed(command[2], process.returncode, output)
                job.output = output
                if self.config_path is None:
                    raise RuntimeError("Agent configuration path is unavailable for reload")
                self.replace_config(load_agent_config(self.config_path))
                job.state = "succeeded"
            except Exception as exc:
                LOG.exception("Provisioning job %s failed", job.id)
                job.error = str(exc)[-MAX_OUTPUT:]
                job.state = "failed"
            finally:
                job.completed_at = time.time()

    def start_provision_job(self, payload: dict) -> Job:
        while len(self.jobs) >= MAX_JOBS:
            self.jobs.popitem(last=False)
        job = Job(
            id=str(uuid.uuid4()),
            server_id=str(payload.get("id", "")),
            operation="provision",
        )
        self.jobs[job.id] = job
        asyncio.create_task(self.run_provision_job(job, payload))
        return job


def create_agent_app(config: AgentConfig, config_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        discovery_task = (
            asyncio.create_task(
                advertise_agent(
                    config.instance_id or config.name,
                    config.name,
                    config.port,
                    config.discovery_port,
                )
            )
            if config.discovery_enabled
            else None
        )
        try:
            yield
        finally:
            if discovery_task is not None:
                discovery_task.cancel()
                await asyncio.gather(discovery_task, return_exceptions=True)

    app = FastAPI(
        title=f"Minecraft Agent - {config.name}",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    runtime = AgentRuntime(config, config_path)
    app.state.runtime = runtime

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {config.token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid agent token",
            )

    def find_server(server_id: str) -> AgentServer:
        server = runtime.servers.get(server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Unknown server")
        return server

    def find_file_manager(server_id: str) -> tuple[AgentServer, ServerFileManager]:
        server = find_server(server_id)
        manager = runtime.file_managers.get(server_id)
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="File manager is not enabled for this server",
            )
        return server, manager

    def file_error(exc: FileManagerError) -> HTTPException:
        return HTTPException(status_code=exc.status_code, detail=str(exc))

    @app.get("/v1/health")
    async def health() -> dict:
        return {"ok": True, "agent": config.name}

    @app.get("/v1/info", dependencies=[Depends(authenticate)])
    async def info() -> dict:
        return {
            "id": config.instance_id or config.name,
            "name": config.name,
            "provisioning_enabled": config.provisioning_enabled,
            "server_types": ["paper", "vanilla", "forge", "neoforge"],
            "servers": [
                {
                    "id": server.id,
                    "name": server.name,
                    "track_players": server.player_query is not None,
                }
                for server in runtime.config.servers
            ],
        }

    @app.get("/v1/catalog/{server_type}", dependencies=[Depends(authenticate)])
    async def catalog(server_type: str) -> dict:
        if not config.provisioning_enabled:
            raise HTTPException(status_code=409, detail="Agent provisioning is disabled")
        try:
            versions = await asyncio.to_thread(list_versions, server_type)
        except CatalogError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "type": server_type,
            "versions": [version.as_dict() for version in versions],
        }

    @app.post("/v1/provision", dependencies=[Depends(authenticate)])
    async def provision(payload: ProvisionRequest) -> dict:
        if not config.provisioning_enabled:
            raise HTTPException(status_code=409, detail="Agent provisioning is disabled")
        if runtime.config_path is None:
            raise HTTPException(status_code=409, detail="Agent cannot reload its configuration")
        if runtime.provisioning_lock.locked():
            raise HTTPException(status_code=409, detail="Another server is being installed")
        if payload.id in runtime.servers:
            raise HTTPException(status_code=409, detail="Server id already exists")
        return asdict(runtime.start_provision_job(payload.model_dump()))

    @app.get("/v1/servers", dependencies=[Depends(authenticate)])
    async def list_servers() -> list[dict]:
        results = []
        for server in runtime.config.servers:
            state = "unknown"
            detail = ""
            if runtime.locks[server.id].locked():
                state = "busy"
            else:
                try:
                    detail = await runtime.execute_steps(
                        server, server.actions["status"], min(server.timeout_seconds, 30)
                    )
                    state = "online"
                except RuntimeError as exc:
                    state = "offline"
                    detail = str(exc)[-1000:]
            results.append(
                {
                    "id": server.id,
                    "name": server.name,
                    "state": state,
                    "detail": detail,
                    "actions": sorted(server.actions.keys() - {"status"}),
                    "scripts": sorted(server.scripts.keys()),
                    "files_enabled": server.file_manager is not None,
                    "player_tracking_available": server.player_query is not None,
                }
            )
        return results

    @app.get(
        "/v1/servers/{server_id}/files",
        dependencies=[Depends(authenticate)],
    )
    async def list_files(server_id: str, path: str = "") -> dict:
        _, manager = find_file_manager(server_id)
        try:
            return manager.list_directory(path)
        except FileManagerError as exc:
            raise file_error(exc) from exc

    @app.get(
        "/v1/servers/{server_id}/files/content",
        dependencies=[Depends(authenticate)],
    )
    async def read_file(server_id: str, path: str) -> dict:
        _, manager = find_file_manager(server_id)
        try:
            return manager.read_text(path)
        except FileManagerError as exc:
            raise file_error(exc) from exc

    @app.get(
        "/v1/servers/{server_id}/files/download",
        dependencies=[Depends(authenticate)],
    )
    async def download_file(server_id: str, path: str) -> StreamingResponse:
        _, manager = find_file_manager(server_id)
        try:
            download = manager.open_download(path)
        except FileManagerError as exc:
            raise file_error(exc) from exc
        encoded_name = quote(download.name, safe="")
        return StreamingResponse(
            download.iter_chunks(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{encoded_name}"
                ),
                "Content-Length": str(download.size),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(download.close),
        )

    @app.put(
        "/v1/servers/{server_id}/files/content",
        dependencies=[Depends(authenticate)],
    )
    async def write_file(server_id: str, payload: FileWriteRequest) -> dict:
        server, manager = find_file_manager(server_id)
        if runtime.locks[server.id].locked():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Server maintenance is in progress; try again when it finishes",
            )
        async with runtime.locks[server.id]:
            try:
                result = manager.write_text(
                    payload.path, payload.content, payload.expected_version
                )
            except FileManagerError as exc:
                raise file_error(exc) from exc
        LOG.info("Saved managed file %s:%s", server.id, result["path"])
        return result

    @app.post(
        "/v1/servers/{server_id}/files/directory",
        dependencies=[Depends(authenticate)],
    )
    async def create_directory(
        server_id: str, payload: DirectoryCreateRequest
    ) -> dict:
        server, manager = find_file_manager(server_id)
        if runtime.locks[server.id].locked():
            raise HTTPException(status_code=409, detail="Server maintenance is in progress")
        async with runtime.locks[server.id]:
            try:
                result = manager.create_directory(payload.path)
            except FileManagerError as exc:
                raise file_error(exc) from exc
        LOG.info("Created managed directory %s:%s", server.id, result["path"])
        return result

    @app.put(
        "/v1/servers/{server_id}/files/upload",
        dependencies=[Depends(authenticate)],
    )
    async def upload_file(
        server_id: str,
        request: Request,
        path: str,
        overwrite: bool = False,
    ) -> dict:
        server, manager = find_file_manager(server_id)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
            if declared_size > manager.config.max_upload_size_bytes:
                raise HTTPException(status_code=413, detail="Upload exceeds configured limit")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > manager.config.max_upload_size_bytes:
                raise HTTPException(status_code=413, detail="Upload exceeds configured limit")
        if runtime.locks[server.id].locked():
            raise HTTPException(status_code=409, detail="Server maintenance is in progress")
        async with runtime.locks[server.id]:
            try:
                result = manager.upload(path, bytes(content), overwrite=overwrite)
            except FileManagerError as exc:
                raise file_error(exc) from exc
        LOG.info("Uploaded managed file %s:%s", server.id, result["path"])
        return result

    @app.get(
        "/v1/servers/{server_id}/players",
        dependencies=[Depends(authenticate)],
    )
    async def list_players(server_id: str) -> dict:
        server = find_server(server_id)
        query = server.player_query
        if query is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Player Query is not configured for this server",
            )

        try:
            players = await query_players(
                query.host,
                query.port,
                query.timeout_seconds,
            )
        except MinecraftQueryError as query_error:
            # A stopped service is a reliable empty snapshot. If the service is
            # still active, preserve the distinction between "no players" and
            # "the read-only Query endpoint is unavailable".
            try:
                await runtime.execute_steps(
                    server,
                    server.actions["status"],
                    min(server.timeout_seconds, 30),
                )
            except CommandFailed as status_error:
                if status_error.returncode in query.offline_status_codes:
                    return {
                        "id": server.id,
                        "name": server.name,
                        "state": "offline",
                        "players": [],
                    }
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "Minecraft Query is unavailable and the status command "
                        f"failed unexpectedly: {str(status_error)[-500:]}"
                    ),
                ) from status_error
            except RuntimeError as status_error:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "Minecraft Query is unavailable and server status could "
                        f"not be confirmed: {str(status_error)[-500:]}"
                    ),
                ) from status_error
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Minecraft Query is unavailable: {query_error}",
            ) from query_error

        return {
            "id": server.id,
            "name": server.name,
            "state": "online",
            "players": list(players),
        }

    @app.post("/v1/servers/{server_id}/actions/{action}", dependencies=[Depends(authenticate)])
    async def start_action(server_id: str, action: str) -> dict:
        server = find_server(server_id)
        if action not in {"start", "stop", "restart", "update"}:
            raise HTTPException(status_code=404, detail="Unsupported action")
        steps = server.actions.get(action)
        if not steps:
            raise HTTPException(status_code=409, detail=f"{action} is not configured")
        timeout = (
            server.update_timeout_seconds if action == "update" else server.timeout_seconds
        )
        return asdict(runtime.start_job(server, action, steps, timeout))

    @app.post(
        "/v1/servers/{server_id}/scripts/{script_name}",
        dependencies=[Depends(authenticate)],
    )
    async def start_script(server_id: str, script_name: str) -> dict:
        server = find_server(server_id)
        steps = server.scripts.get(script_name)
        if not steps:
            raise HTTPException(status_code=404, detail="Unknown or disallowed script")
        return asdict(
            runtime.start_job(
                server, f"script:{script_name}", steps, server.update_timeout_seconds
            )
        )

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(authenticate)])
    async def get_job(job_id: str) -> dict:
        job = runtime.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job")
        return asdict(job)

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Minecraft host agent")
    parser.add_argument(
        "--config",
        default=os.getenv("MC_AGENT_CONFIG", "config/agent.toml"),
        help="Path to agent TOML configuration",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    try:
        config_path = Path(args.config)
        config = load_agent_config(config_path)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    uvicorn.run(create_agent_app(config, config_path), host=config.bind, port=config.port)


if __name__ == "__main__":
    main()

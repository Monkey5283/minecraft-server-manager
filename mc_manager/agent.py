from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel

from .config import AgentConfig, AgentServer, ConfigError, load_agent_config
from .file_manager import FileManagerError, ServerFileManager
from .minecraft_query import MinecraftQueryError, query_players


LOG = logging.getLogger("mc_manager.agent")
MAX_OUTPUT = 32_000
MAX_JOBS = 200


class FileWriteRequest(BaseModel):
    path: str
    content: str
    expected_version: str | None = None


class DirectoryCreateRequest(BaseModel):
    path: str


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
    def __init__(self, config: AgentConfig):
        self.config = config
        self.servers = {server.id: server for server in config.servers}
        self.locks = {server.id: asyncio.Lock() for server in config.servers}
        self.file_managers = {
            server.id: ServerFileManager(server.file_manager)
            for server in config.servers
            if server.file_manager is not None
        }
        self.jobs: OrderedDict[str, Job] = OrderedDict()

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


def create_agent_app(config: AgentConfig) -> FastAPI:
    app = FastAPI(title=f"Minecraft Agent - {config.name}", docs_url=None, redoc_url=None)
    runtime = AgentRuntime(config)
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

    @app.get("/v1/servers", dependencies=[Depends(authenticate)])
    async def list_servers() -> list[dict]:
        results = []
        for server in config.servers:
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
        config = load_agent_config(Path(args.config))
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    uvicorn.run(create_agent_app(config), host=config.bind, port=config.port)


if __name__ == "__main__":
    main()

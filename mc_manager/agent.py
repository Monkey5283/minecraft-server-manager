from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
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
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .config import AgentConfig, AgentServer, ConfigError, load_agent_config
from .discovery import advertise_agent
from .file_manager import FileManagerError, ServerFileManager
from .minecraft_query import MinecraftQueryError, query_players
from .server_catalog import CatalogError, list_versions
from .server_console import ServerConsole, ServerConsoleError


LOG = logging.getLogger("mc_manager.agent")
MAX_OUTPUT = 32_000
MAX_JOBS = 200
STANDARD_MINECRAFT_ROOT = Path("/srv/minecraft")


class FileWriteRequest(BaseModel):
    path: str
    content: str
    expected_version: str | None = None


class DirectoryCreateRequest(BaseModel):
    path: str


class ConsoleCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4096)


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
        self.maintenance_servers: set[str] = set()
        self.servers = {server.id: server for server in config.servers}
        self.locks = {server.id: asyncio.Lock() for server in config.servers}
        self.file_managers = {
            server.id: ServerFileManager(server.file_manager)
            for server in config.servers
            if server.file_manager is not None
        }
        self.consoles = {
            server.id: ServerConsole(server.console)
            for server in config.servers
            if server.console is not None
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
        self.consoles = {
            server.id: ServerConsole(server.console)
            for server in config.servers
            if server.console is not None
        }

    def managed_server_record(self, server_id: str) -> dict | None:
        try:
            payload = json.loads(
                self.config.managed_servers_file.read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        entries = payload.get("servers", []) if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            return None
        return next(
            (
                item
                for item in entries
                if isinstance(item, dict) and item.get("id") == server_id
            ),
            None,
        )

    def standard_legacy_server(self, server: AgentServer) -> bool:
        if self.managed_server_record(server.id) is not None:
            return False
        expected_directory = (STANDARD_MINECRAFT_ROOT / server.id).resolve()
        service = f"minecraft@{server.id}.service"
        has_standard_stop = any(
            len(command) >= 3
            and command[-3:] == ("/usr/bin/systemctl", "stop", service)
            for command in server.actions.get("stop", ())
        )
        return server.working_directory == expected_directory and has_standard_stop

    def software_metadata(self, server: AgentServer) -> dict | None:
        record = self.managed_server_record(server.id)
        if record is None:
            return None
        raw = record.get("software")
        if isinstance(raw, dict):
            return {
                "type": str(raw.get("type", "")),
                "version": str(raw.get("version", "")),
                "java_path": str(raw.get("java_path", "/usr/bin/java")),
                "minimum_memory": str(raw.get("minimum_memory", "1G")),
                "maximum_memory": str(raw.get("maximum_memory", "4G")),
            }

        metadata = {
            "type": "paper" if "update" in server.actions else "",
            "version": "",
            "java_path": "/usr/bin/java",
            "minimum_memory": "1G",
            "maximum_memory": "4G",
        }
        update_environment = server.working_directory / ".manager-update.env"
        try:
            for line in update_environment.read_text(encoding="utf-8").splitlines():
                if line.startswith("PAPER_VERSION="):
                    metadata["type"] = "paper"
                    metadata["version"] = line.partition("=")[2]
        except (FileNotFoundError, OSError, UnicodeError):
            pass
        try:
            launcher = (server.working_directory / "start-server").read_text(
                encoding="utf-8"
            )
            java_match = re.search(r"^exec\s+(\S+)\s+-Xms", launcher, re.MULTILINE)
            minimum_match = re.search(r"-Xms([^\s]+)", launcher)
            maximum_match = re.search(r"-Xmx([^\s]+)", launcher)
            if java_match:
                metadata["java_path"] = java_match.group(1)
            if minimum_match:
                metadata["minimum_memory"] = minimum_match.group(1).upper()
            if maximum_match:
                metadata["maximum_memory"] = maximum_match.group(1).upper()
        except (FileNotFoundError, OSError, UnicodeError):
            pass
        return metadata

    def server_busy(self, server_id: str) -> bool:
        return (
            server_id in self.maintenance_servers
            or self.locks[server_id].locked()
        )

    def has_pending_job(self, server_id: str) -> bool:
        return any(
            job.server_id == server_id and job.state in {"queued", "running"}
            for job in self.jobs.values()
        )

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

    async def run_software_change_job(
        self, job: Job, server: AgentServer, payload: dict
    ) -> None:
        async with self.provisioning_lock, self.locks[server.id]:
            job.state = "running"
            job.started_at = time.time()
            command = (
                "sudo",
                "-n",
                "/opt/minecraft-manager/venv/bin/mc-manager-change-software",
                "--request",
                json.dumps(
                    {"id": server.id, **payload}, separators=(",", ":")
                ),
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=os.environ.copy(),
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=7200)
                output = stdout.decode("utf-8", errors="replace")[-MAX_OUTPUT:]
                if process.returncode != 0:
                    raise CommandFailed(command[2], process.returncode, output)
                job.output = output
                if self.config_path is None:
                    raise RuntimeError("Agent configuration path is unavailable for reload")
                self.replace_config(load_agent_config(self.config_path))
                job.state = "succeeded"
            except Exception as exc:
                LOG.exception("Software change job %s failed", job.id)
                job.error = str(exc)[-MAX_OUTPUT:]
                job.state = "failed"
            finally:
                self.maintenance_servers.discard(server.id)
                job.completed_at = time.time()

    def start_software_change_job(
        self, server: AgentServer, payload: dict
    ) -> Job:
        while len(self.jobs) >= MAX_JOBS:
            self.jobs.popitem(last=False)
        job = Job(
            id=str(uuid.uuid4()),
            server_id=server.id,
            operation="change_software",
        )
        self.jobs[job.id] = job
        self.maintenance_servers.add(server.id)
        asyncio.create_task(self.run_software_change_job(job, server, payload))
        return job

    async def run_delete_server_job(
        self,
        job: Job,
        server: AgentServer,
        confirmation: str,
        legacy: bool,
    ) -> None:
        async with self.provisioning_lock, self.locks[server.id]:
            job.state = "running"
            job.started_at = time.time()
            command = (
                "sudo",
                "-n",
                "/opt/minecraft-manager/venv/bin/mc-manager-delete-server",
                "--request",
                json.dumps(
                    {
                        "id": server.id,
                        "confirmation": confirmation,
                        "legacy": legacy,
                    },
                    separators=(",", ":"),
                ),
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=os.environ.copy(),
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=7200)
                output = stdout.decode("utf-8", errors="replace")[-MAX_OUTPUT:]
                if process.returncode != 0:
                    raise CommandFailed(command[2], process.returncode, output)
                job.output = output
                if self.config_path is None:
                    raise RuntimeError("Agent configuration path is unavailable for reload")
                self.replace_config(load_agent_config(self.config_path))
                job.state = "succeeded"
            except Exception as exc:
                LOG.exception("Server deletion job %s failed", job.id)
                job.error = str(exc)[-MAX_OUTPUT:]
                job.state = "failed"
            finally:
                self.maintenance_servers.discard(server.id)
                job.completed_at = time.time()

    def start_delete_server_job(
        self, server: AgentServer, confirmation: str, *, legacy: bool = False
    ) -> Job:
        while len(self.jobs) >= MAX_JOBS:
            self.jobs.popitem(last=False)
        job = Job(
            id=str(uuid.uuid4()),
            server_id=server.id,
            operation="delete_server",
        )
        self.jobs[job.id] = job
        self.maintenance_servers.add(server.id)
        asyncio.create_task(
            self.run_delete_server_job(job, server, confirmation, legacy)
        )
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

    def find_console(server_id: str) -> tuple[AgentServer, ServerConsole]:
        server = find_server(server_id)
        console = runtime.consoles.get(server_id)
        if console is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Minecraft console is not enabled for this server",
            )
        return server, console

    def file_error(exc: FileManagerError) -> HTTPException:
        return HTTPException(status_code=exc.status_code, detail=str(exc))

    def console_error(exc: ServerConsoleError) -> HTTPException:
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

    @app.post(
        "/v1/servers/{server_id}/software",
        dependencies=[Depends(authenticate)],
    )
    async def change_software(
        server_id: str, payload: SoftwareChangeRequest
    ) -> dict:
        if not runtime.config.provisioning_enabled:
            raise HTTPException(status_code=409, detail="Agent provisioning is disabled")
        if runtime.config_path is None:
            raise HTTPException(status_code=409, detail="Agent cannot reload its configuration")
        server = find_server(server_id)
        if runtime.managed_server_record(server_id) is None:
            raise HTTPException(
                status_code=409,
                detail="Only dashboard-provisioned servers can change software",
            )
        if (
            runtime.provisioning_lock.locked()
            or runtime.server_busy(server.id)
            or runtime.has_pending_job(server.id)
        ):
            raise HTTPException(status_code=409, detail="Server maintenance is in progress")
        return asdict(
            runtime.start_software_change_job(server, payload.model_dump())
        )

    @app.delete(
        "/v1/servers/{server_id}",
        dependencies=[Depends(authenticate)],
    )
    async def delete_server(server_id: str, payload: DeleteServerRequest) -> dict:
        if not runtime.config.provisioning_enabled:
            raise HTTPException(status_code=409, detail="Agent provisioning is disabled")
        if runtime.config_path is None:
            raise HTTPException(status_code=409, detail="Agent cannot reload its configuration")
        server = find_server(server_id)
        managed = runtime.managed_server_record(server_id) is not None
        legacy = runtime.standard_legacy_server(server)
        if not managed and not legacy:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Legacy deletion requires the standard "
                    "/srv/minecraft/SERVER_ID directory and minecraft@ service"
                ),
            )
        if not hmac.compare_digest(payload.confirmation, server.id):
            raise HTTPException(
                status_code=400,
                detail="Deletion confirmation must exactly match the server id",
            )
        if (
            runtime.provisioning_lock.locked()
            or runtime.server_busy(server.id)
            or runtime.has_pending_job(server.id)
        ):
            raise HTTPException(status_code=409, detail="Server maintenance is in progress")
        return asdict(
            runtime.start_delete_server_job(
                server, payload.confirmation, legacy=legacy
            )
        )

    @app.get("/v1/servers", dependencies=[Depends(authenticate)])
    async def list_servers() -> list[dict]:
        results = []
        for server in runtime.config.servers:
            state = "unknown"
            detail = ""
            if runtime.server_busy(server.id):
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
            software = runtime.software_metadata(server)
            deletion_supported = (
                runtime.managed_server_record(server.id) is not None
                or runtime.standard_legacy_server(server)
            )
            results.append(
                {
                    "id": server.id,
                    "name": server.name,
                    "state": state,
                    "detail": detail,
                    "actions": sorted(server.actions.keys() - {"status"}),
                    "scripts": sorted(server.scripts.keys()),
                    "files_enabled": server.file_manager is not None,
                    "console_enabled": server.console is not None,
                    "player_tracking_available": server.player_query is not None,
                    "software_change_enabled": (
                        runtime.config.provisioning_enabled and software is not None
                    ),
                    "deletion_enabled": (
                        runtime.config.provisioning_enabled and deletion_supported
                    ),
                    "software": software,
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
        if runtime.server_busy(server.id):
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
        if runtime.server_busy(server.id):
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
        if runtime.server_busy(server.id):
            raise HTTPException(status_code=409, detail="Server maintenance is in progress")
        async with runtime.locks[server.id]:
            try:
                result = manager.upload(path, bytes(content), overwrite=overwrite)
            except FileManagerError as exc:
                raise file_error(exc) from exc
        LOG.info("Uploaded managed file %s:%s", server.id, result["path"])
        return result

    @app.delete(
        "/v1/servers/{server_id}/files",
        dependencies=[Depends(authenticate)],
    )
    async def delete_file(server_id: str, path: str) -> dict:
        server, manager = find_file_manager(server_id)
        if runtime.server_busy(server.id):
            raise HTTPException(status_code=409, detail="Server maintenance is in progress")
        async with runtime.locks[server.id]:
            try:
                result = manager.delete(path)
            except FileManagerError as exc:
                raise file_error(exc) from exc
        LOG.warning("Deleted managed %s %s:%s", result["kind"], server.id, result["path"])
        return result

    @app.get(
        "/v1/servers/{server_id}/console",
        dependencies=[Depends(authenticate)],
    )
    async def console_output(server_id: str, cursor: int = 0) -> dict:
        _, console = find_console(server_id)
        try:
            return await asyncio.to_thread(console.read_output, cursor)
        except ServerConsoleError as exc:
            raise console_error(exc) from exc

    @app.post(
        "/v1/servers/{server_id}/console",
        dependencies=[Depends(authenticate)],
    )
    async def console_command(server_id: str, payload: ConsoleCommandRequest) -> dict:
        server, console = find_console(server_id)
        try:
            result = await asyncio.to_thread(console.send_command, payload.command)
        except ServerConsoleError as exc:
            raise console_error(exc) from exc
        fingerprint = hashlib.sha256(payload.command.encode("utf-8")).hexdigest()[:12]
        LOG.warning(
            "Accepted Minecraft console command server=%s verb=%s fingerprint=%s",
            server.id,
            result["command"],
            fingerprint,
        )
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
        if runtime.server_busy(server.id):
            raise HTTPException(status_code=409, detail="Server maintenance is in progress")
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
        if runtime.server_busy(server.id):
            raise HTTPException(status_code=409, detail="Server maintenance is in progress")
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

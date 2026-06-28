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
from fastapi import Depends, FastAPI, Header, HTTPException, status

from .config import AgentConfig, AgentServer, ConfigError, load_agent_config


LOG = logging.getLogger("mc_manager.agent")
MAX_OUTPUT = 32_000
MAX_JOBS = 200


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
                raise RuntimeError(
                    f"Command {command[0]} exited with code {process.returncode}\n{text}"
                )
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
                }
            )
        return results

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

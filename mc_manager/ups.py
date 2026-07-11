from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .client import AgentClient, AgentUnavailable
from .config import ControllerConfig, RemoteServer, UPSConfig


LOG = logging.getLogger("mc_manager.ups")


CommandRunner = Callable[[tuple[str, ...]], Awaitable[str]]
Announcer = Callable[[str], Awaitable[None]]
Sleeper = Callable[[float], Awaitable[None]]


def is_on_battery(status: str) -> bool:
    tokens = {part.strip().upper() for part in status.replace(",", " ").split()}
    return bool(tokens.intersection({"OB", "LB"}))


async def run_command(command: tuple[str, ...]) -> str:
    LOG.info("Running UPS command: %s", command[0])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    output = stdout.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(
            f"{command[0]} exited with code {process.returncode}: {output[-1000:]}"
        )
    return output


async def ups_status_message(
    ups: UPSConfig,
    command_runner: CommandRunner = run_command,
) -> str:
    status = (await command_runner(ups.status_command)).strip() or "unknown"
    try:
        charge = (await command_runner(ups.charge_command)).strip()
    except Exception:
        LOG.exception("Could not read UPS battery charge")
        charge = "unknown"
    power = "On battery" if is_on_battery(status) else "Online / line power"
    charge_text = f"{charge}%" if charge != "unknown" else charge
    return (
        "**Battery Backup**\n"
        f"Power: **{power}** (`{status}`)\n"
        f"Battery: **{charge_text}**"
    )


class UPSMonitor:
    def __init__(
        self,
        config: ControllerConfig,
        agents: AgentClient,
        announce: Announcer,
        *,
        command_runner: CommandRunner | None = None,
        sleeper: Sleeper = asyncio.sleep,
    ):
        self.config = config
        self.ups = config.ups
        self.agents = agents
        self.announce = announce
        self.command_runner = command_runner or run_command
        self.sleep = sleeper
        self._triggered = False

    async def run(self) -> None:
        if not self.ups.enabled:
            return
        LOG.info("UPS monitor enabled for %s", self.ups.ups_name)
        while not self._triggered:
            try:
                status = await self.read_status()
            except Exception:
                LOG.exception("Could not read UPS status")
                await self.sleep(self.ups.poll_interval_seconds)
                continue

            if self.is_on_battery(status):
                LOG.warning("UPS %s is on battery: %s", self.ups.ups_name, status)
                if self.ups.on_battery_delay_seconds:
                    await self.sleep(self.ups.on_battery_delay_seconds)
                    status = await self.read_status()
                    if not self.is_on_battery(status):
                        LOG.info("UPS %s returned to line power", self.ups.ups_name)
                        continue
                self._triggered = True
                await self.handle_power_outage(status)
                return

            await self.sleep(self.ups.poll_interval_seconds)

    async def read_status(self) -> str:
        output = await self.command_runner(self.ups.status_command)
        return output.strip()

    @staticmethod
    def is_on_battery(status: str) -> bool:
        return is_on_battery(status)

    async def handle_power_outage(self, status: str) -> None:
        await self.announce(
            "⚠️ **UPS is on battery.** Power outage detected "
            f"(`{status}`). Stopping Minecraft servers now."
        )
        results = await asyncio.gather(
            *(self._protect_server(server) for server in self.config.servers),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            LOG.error("UPS shutdown sequence had %s server failure(s)", len(failures))

        await self.announce(
            "⚠️ UPS shutdown sequence finished. The Raspberry Pi controller "
            f"will shut down in {self.ups.local_shutdown_delay_seconds} seconds."
        )
        if self.ups.local_shutdown_delay_seconds:
            await self.sleep(self.ups.local_shutdown_delay_seconds)
        await self.command_runner(self.ups.local_shutdown_command)

    async def _protect_server(self, server: RemoteServer) -> None:
        try:
            await self.announce(f"🛑 Stopping **{server.name}** because the UPS is on battery.")
            stop_job = await self.agents.action(server, "stop")
            await self._wait_for_job(server, stop_job["id"], self.ups.stop_timeout_seconds)
            await self._shutdown_downstream_host(server)
        except AgentUnavailable as exc:
            LOG.warning("UPS sequence could not reach %s: %s", server.id, exc)
            await self.announce(f"⚠️ **{server.name}** could not be reached: {exc}")
        except Exception as exc:
            LOG.exception("UPS sequence failed for %s", server.id)
            await self.announce(f"⚠️ **{server.name}** UPS shutdown step failed: {exc}")
            raise

    async def _shutdown_downstream_host(self, server: RemoteServer) -> None:
        script_name = self.ups.downstream_shutdown_script
        try:
            status = await self.agents.status(server)
        except AgentUnavailable as exc:
            LOG.info(
                "Skipping downstream host shutdown for %s after stop: %s",
                server.id,
                exc,
            )
            return
        if script_name not in status.get("scripts", []):
            LOG.info("%s does not expose %s script", server.id, script_name)
            await self.announce(
                f"ℹ️ **{server.name}** stopped, but no `{script_name}` script is configured."
            )
            return
        await self.announce(f"⏻ Asking **{server.name}** agent to shut down its host.")
        job = await self.agents.script(server, script_name)
        await self._wait_for_job(server, job["id"], min(60, self.ups.stop_timeout_seconds))

    async def _wait_for_job(
        self, server: RemoteServer, job_id: str, timeout_seconds: int
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            job = await self.agents.job(server, job_id)
            if job["state"] in {"succeeded", "failed"}:
                if job["state"] == "failed":
                    raise RuntimeError(str(job.get("error", "Unknown job failure")))
                return job
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"{server.name}: job {job_id} timed out")
            await self.sleep(2)


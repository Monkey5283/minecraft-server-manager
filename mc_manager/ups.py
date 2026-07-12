from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .client import AgentClient, AgentUnavailable
from .config import ControllerConfig, RemoteServer, UPSConfig


LOG = logging.getLogger("mc_manager.ups")
NUT_QUERY_TIMEOUT_SECONDS = 5.0
UPS_CRITICAL_TOKENS = frozenset({"OB", "LB", "FSD", "OFF"})
UPS_WARNING_TOKENS = frozenset(
    {"RB", "OVER", "BYPASS", "CAL", "DISCHRG", "TRIM", "BOOST"}
)
UPS_POWER_TOKENS = frozenset({"OL", "OB", "LB", "FSD", "OFF", "BYPASS"})


@dataclass(frozen=True)
class UPSReading:
    available: bool
    status: str
    charge_percent: float | None
    observed_at: float

    @property
    def on_battery(self) -> bool:
        return self.available and is_on_battery(self.status)

    @classmethod
    def unavailable(cls, observed_at: float) -> "UPSReading":
        return cls(False, "unknown", None, observed_at)


CommandRunner = Callable[[tuple[str, ...]], Awaitable[str]]
Announcer = Callable[[str], Awaitable[None]]
Sleeper = Callable[[float], Awaitable[None]]
StatusSink = Callable[[UPSReading], None]


def is_on_battery(status: str) -> bool:
    tokens = ups_status_tokens(status)
    return bool(tokens.intersection({"OB", "LB"}))


def requires_protective_shutdown(status: str) -> bool:
    return bool(ups_status_tokens(status).intersection(UPS_CRITICAL_TOKENS))


def has_confirmed_line_power(status: str) -> bool:
    tokens = ups_status_tokens(status)
    return "OL" in tokens and not tokens.intersection(UPS_CRITICAL_TOKENS)


def ups_status_tokens(status: str) -> set[str]:
    return {
        part.strip().upper()
        for part in status.replace(",", " ").split()
        if part.strip()
    }


def has_valid_ups_status(status: str) -> bool:
    tokens = ups_status_tokens(status)
    return "UNKNOWN" not in tokens and bool(tokens.intersection(UPS_POWER_TOKENS))


def ups_power_label(reading: UPSReading) -> str:
    if not reading.available:
        return "Unavailable"
    tokens = ups_status_tokens(reading.status)
    if tokens.intersection({"OB", "LB"}):
        return "On battery"
    if "FSD" in tokens:
        return "Shutdown in progress"
    if "OFF" in tokens:
        return "UPS output off"
    if "BYPASS" in tokens:
        return "Bypass power"
    if "OL" in tokens:
        return "Online / line power"
    return "Unknown"


def is_ups_ready(reading: UPSReading) -> bool:
    tokens = ups_status_tokens(reading.status)
    return (
        reading.available
        and "OL" in tokens
        and reading.charge_percent is not None
        and not tokens.intersection(UPS_CRITICAL_TOKENS | UPS_WARNING_TOKENS)
    )


def clean_upsc_value(output: str) -> str:
    ignored_lines = {
        "Init SSL without certificate database",
    }
    lines = [
        line.strip()
        for line in output.splitlines()
        if line.strip() and line.strip() not in ignored_lines
    ]
    if not lines:
        return ""
    return lines[-1]


def parse_charge_percent(output: str) -> float | None:
    value = clean_upsc_value(output)
    if not value:
        return None
    try:
        charge = float(value)
    except ValueError:
        return None
    return charge if 0 <= charge <= 100 else None


def format_charge_percent(charge: float | None) -> str:
    return "unknown" if charge is None else f"{charge:g}%"


async def run_command(command: tuple[str, ...]) -> str:
    LOG.info("Running UPS command: %s", command[0])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        raise
    output = stdout.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(
            f"{command[0]} exited with code {process.returncode}: {output[-1000:]}"
        )
    return output


async def run_nut_query(
    command: tuple[str, ...],
    command_runner: CommandRunner,
    *,
    timeout_seconds: float = NUT_QUERY_TIMEOUT_SECONDS,
) -> str:
    try:
        return await asyncio.wait_for(
            command_runner(command),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"{command[0]} did not respond within {timeout_seconds:g} seconds"
        ) from exc


async def read_ups_charge(
    ups: UPSConfig,
    command_runner: CommandRunner,
) -> float | None:
    try:
        output = await run_nut_query(ups.charge_command, command_runner)
    except Exception:
        LOG.warning("Could not read UPS battery charge", exc_info=True)
        return None
    return parse_charge_percent(output)


async def read_ups_reading(
    ups: UPSConfig,
    command_runner: CommandRunner = run_command,
    *,
    clock: Callable[[], float] = time.time,
) -> UPSReading:
    status = clean_upsc_value(
        await run_nut_query(ups.status_command, command_runner)
    )
    observed_at = clock()
    if not has_valid_ups_status(status):
        return UPSReading.unavailable(observed_at)
    charge = await read_ups_charge(ups, command_runner)
    return UPSReading(True, status, charge, observed_at)


async def ups_status_message(
    ups: UPSConfig,
    command_runner: CommandRunner = run_command,
) -> str:
    reading = await read_ups_reading(ups, command_runner)
    power = ups_power_label(reading)
    return (
        "**Battery Backup**\n"
        f"Power: **{power}** (`{reading.status}`)\n"
        f"Battery: **{format_charge_percent(reading.charge_percent)}**"
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
        status_sink: StatusSink | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.ups = config.ups
        self.agents = agents
        self.announce = announce
        self.command_runner = command_runner or run_command
        self.sleep = sleeper
        self.status_sink = status_sink
        self.clock = clock
        self._triggered = False
        self._last_charge: float | None = None

    async def run(self) -> None:
        if not self.ups.enabled:
            return
        LOG.info("UPS monitor enabled for %s", self.ups.ups_name)
        while not self._triggered:
            try:
                status = await self.read_status()
            except Exception:
                LOG.exception("Could not read UPS status")
                self._publish_reading(UPSReading.unavailable(self.clock()))
                await self.sleep(self.ups.poll_interval_seconds)
                continue

            if requires_protective_shutdown(status):
                reading = UPSReading(
                    True,
                    status,
                    self._last_charge,
                    self.clock(),
                )
                # Publish and announce from the safety-critical status query
                # before attempting the optional battery charge query.
                self._publish_reading(reading)
                LOG.warning(
                    "UPS %s requires protective shutdown: %s",
                    self.ups.ups_name,
                    reading.status,
                )
                confirmed_status = await self.confirm_on_battery_after_delay(
                    reading.status
                )
                if confirmed_status is None:
                    continue
                self._triggered = True
                await self.handle_power_outage(confirmed_status)
                return

            charge = await self.read_charge()
            if charge is not None:
                self._last_charge = charge
            self._publish_reading(
                UPSReading(True, status, charge, self.clock())
            )
            await self.sleep(self.ups.poll_interval_seconds)

    async def read_status(self) -> str:
        output = await run_nut_query(
            self.ups.status_command,
            self.command_runner,
        )
        status = clean_upsc_value(output)
        if not has_valid_ups_status(status):
            raise RuntimeError("UPS returned an empty or unknown status")
        return status

    async def read_charge(self) -> float | None:
        return await read_ups_charge(self.ups, self.command_runner)

    async def read_reading(self) -> UPSReading:
        return await read_ups_reading(
            self.ups,
            self.command_runner,
            clock=self.clock,
        )

    def _publish_reading(self, reading: UPSReading) -> None:
        if self.status_sink is None:
            return
        try:
            self.status_sink(reading)
        except Exception:
            LOG.exception("Could not publish UPS status to the health monitor")

    @staticmethod
    def is_on_battery(status: str) -> bool:
        return is_on_battery(status)

    async def confirm_on_battery_after_delay(self, initial_status: str) -> str | None:
        delay = self.ups.on_battery_delay_seconds
        if is_on_battery(initial_status):
            announcement = (
                "⚠️ **UPS is on battery.** Power outage detected "
                f"(`{initial_status}`). Shutdown starts in {delay} seconds unless "
                "power returns."
            )
        else:
            announcement = (
                "⚠️ **UPS entered a critical power state.** "
                f"(`{initial_status}`). Protective shutdown starts in {delay} "
                "seconds unless normal line power returns."
            )
        await self.announce(announcement)
        if not delay:
            return initial_status

        delay_task = asyncio.ensure_future(self.sleep(delay))
        charge_task = asyncio.create_task(self.read_charge())
        try:
            done, _pending = await asyncio.wait(
                {delay_task, charge_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if charge_task in done:
                charge = charge_task.result()
                if charge is not None:
                    self._last_charge = charge
                    self._publish_reading(
                        UPSReading(True, initial_status, charge, self.clock())
                    )
            else:
                charge_task.cancel()
                await asyncio.gather(charge_task, return_exceptions=True)
            await delay_task
        finally:
            unfinished = [
                task for task in (delay_task, charge_task) if not task.done()
            ]
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)

        try:
            status = await self.read_status()
        except Exception:
            LOG.exception(
                "Could not confirm UPS status after the on-battery delay; "
                "continuing with fail-safe shutdown"
            )
            self._publish_reading(UPSReading.unavailable(self.clock()))
            await self.announce(
                "⚠️ **UPS status could not be confirmed after the delay.** "
                "Continuing the protective shutdown because the last confirmed "
                "state was on battery."
            )
            return initial_status

        reading = UPSReading(True, status, self._last_charge, self.clock())
        self._publish_reading(reading)
        if has_confirmed_line_power(reading.status):
            LOG.info("UPS %s returned to line power", self.ups.ups_name)
            await self.announce(
                "✅ **UPS is back on line power.** Shutdown sequence canceled "
                f"(`{reading.status}`)."
            )
            return None

        LOG.warning(
            "UPS %s did not confirm normal line power after the delay: %s",
            self.ups.ups_name,
            reading.status,
        )
        return reading.status

    async def handle_power_outage(self, status: str) -> None:
        await self.announce(
            "⚠️ **UPS still requires protective shutdown after the delay.** "
            f"Stopping Minecraft servers now (`{status}`)."
        )
        stop_results = await asyncio.gather(
            *(self._stop_server(server) for server in self.config.servers),
            return_exceptions=True,
        )
        failures = [
            result for result in stop_results if isinstance(result, BaseException)
        ]
        if failures:
            LOG.error("UPS stop phase had %s server failure(s)", len(failures))

        groups: dict[tuple[str, str], list[tuple[RemoteServer, bool]]] = {}
        for server, result in zip(
            self.config.servers,
            stop_results,
            strict=True,
        ):
            groups.setdefault((server.agent_url, server.token), []).append(
                (server, not isinstance(result, BaseException))
            )

        host_results = await asyncio.gather(
            *(self._protect_downstream_host(group) for group in groups.values()),
            return_exceptions=True,
        )
        host_failures = [
            result for result in host_results if isinstance(result, BaseException)
        ]
        if host_failures:
            LOG.error(
                "UPS host shutdown phase had %s failure(s)",
                len(host_failures),
            )

        await self.announce(
            "⚠️ UPS shutdown sequence finished. The Raspberry Pi controller "
            f"will shut down in {self.ups.local_shutdown_delay_seconds} seconds."
        )
        if self.ups.local_shutdown_delay_seconds:
            await self.sleep(self.ups.local_shutdown_delay_seconds)
        await self.command_runner(self.ups.local_shutdown_command)

    async def _stop_server(self, server: RemoteServer) -> None:
        try:
            await self.announce(f"🛑 Stopping **{server.name}** because the UPS is on battery.")
            stop_job = await self.agents.action(server, "stop")
            await self._wait_for_job(server, stop_job["id"], self.ups.stop_timeout_seconds)
        except AgentUnavailable as exc:
            LOG.warning("UPS sequence could not reach %s: %s", server.id, exc)
            await self.announce(f"⚠️ **{server.name}** could not be reached: {exc}")
            raise
        except Exception as exc:
            LOG.exception("UPS sequence failed for %s", server.id)
            await self.announce(f"⚠️ **{server.name}** UPS shutdown step failed: {exc}")
            raise

    async def _protect_downstream_host(
        self,
        group: list[tuple[RemoteServer, bool]],
    ) -> None:
        servers = [server for server, _stopped in group]
        failed = [server for server, stopped in group if not stopped]
        host_label = ", ".join(server.name for server in servers)
        if failed:
            await self.announce(
                f"⚠️ Not shutting down the host for **{host_label}** because "
                "one or more services did not stop cleanly."
            )
            return

        script_name = self.ups.downstream_shutdown_script
        try:
            statuses = await self.agents.statuses(servers[0])
        except AgentUnavailable as exc:
            LOG.info(
                "Skipping downstream host shutdown for %s after stops: %s",
                ",".join(server.id for server in servers),
                exc,
            )
            await self.announce(
                f"⚠️ **{host_label}** stopped, but its agent could not be "
                f"reached for host shutdown: {exc}"
            )
            return

        target = next(
            (
                server
                for server in servers
                if script_name in statuses.get(server.id, {}).get("scripts", [])
            ),
            None,
        )
        if target is None:
            LOG.info(
                "%s does not expose %s script",
                ",".join(server.id for server in servers),
                script_name,
            )
            await self.announce(
                f"ℹ️ **{host_label}** stopped, but no `{script_name}` script "
                "is configured for that host."
            )
            return
        await self.announce(
            f"⏻ Asking the **{host_label}** agent to shut down its host."
        )
        try:
            job = await self.agents.script(target, script_name)
            await self._wait_for_job(
                target,
                job["id"],
                min(60, self.ups.stop_timeout_seconds),
            )
        except Exception as exc:
            LOG.exception("Could not shut down downstream host for %s", host_label)
            await self.announce(
                f"⚠️ **{host_label}** stopped, but its host shutdown failed: {exc}"
            )
            raise

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


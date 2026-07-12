from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

from .client import AgentClient
from .config import ControllerConfig, RemoteServer
from .ups import (
    UPSReading,
    UPS_CRITICAL_TOKENS,
    UPS_POWER_TOKENS,
    UPS_WARNING_TOKENS,
    ups_status_tokens,
)


LOG = logging.getLogger("mc_manager.health")


class HealthLevel(IntEnum):
    ALL_GOOD = 0
    CAUTION = 1
    ATTENTION = 2

    @property
    def label(self) -> str:
        if self is HealthLevel.ALL_GOOD:
            return "All Good"
        if self is HealthLevel.CAUTION:
            return "Caution"
        return "Attention"

    @property
    def icon(self) -> str:
        if self is HealthLevel.ALL_GOOD:
            return "🟢"
        if self is HealthLevel.CAUTION:
            return "🟡"
        return "🔴"


@dataclass(frozen=True)
class ServerHealth:
    id: str
    name: str
    state: str


@dataclass(frozen=True)
class ControllerHealthSnapshot:
    level: HealthLevel
    servers: tuple[ServerHealth, ...]
    ups: UPSReading | None
    observed_at: float


class HealthPublisher(Protocol):
    async def publish_health(self, snapshot: ControllerHealthSnapshot) -> None: ...


Sleeper = Callable[[float], Awaitable[None]]


def assess_health(
    servers: Sequence[ServerHealth],
    ups_enabled: bool,
    ups: UPSReading | None,
) -> HealthLevel:
    """Assess current observations without adding debounce or historical state."""

    level = HealthLevel.ALL_GOOD
    for server in servers:
        state = server.state.strip().lower()
        if state in {"offline", "busy"}:
            level = max(level, HealthLevel.CAUTION)
        elif state != "online":
            level = max(level, HealthLevel.ATTENTION)

    if not ups_enabled:
        return level
    if ups is None or not ups.available:
        return HealthLevel.ATTENTION

    tokens = ups_status_tokens(ups.status)
    if "UNKNOWN" in tokens or not tokens.intersection(UPS_POWER_TOKENS):
        return HealthLevel.ATTENTION
    if tokens.intersection(UPS_CRITICAL_TOKENS):
        return HealthLevel.ATTENTION
    if ups.charge_percent is None or tokens.intersection(UPS_WARNING_TOKENS):
        level = max(level, HealthLevel.CAUTION)
    return level


class ControllerHealthMonitor:
    def __init__(
        self,
        config: ControllerConfig,
        agents: AgentClient,
        publisher: HealthPublisher,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self.config = config
        self.agents = agents
        self.publisher = publisher
        self.clock = clock
        self.sleep = sleeper
        self._servers = tuple(
            ServerHealth(server.id, server.name, "unknown")
            for server in config.servers
        )
        self._ups: UPSReading | None = None
        self._ups_updated = asyncio.Event()
        self.latest_snapshot: ControllerHealthSnapshot | None = None

    def update_ups(self, reading: UPSReading) -> None:
        """Store a UPS reading and wake the monitor without blocking its caller."""

        self._ups = reading
        self._ups_updated.set()

    async def poll_once(self) -> ControllerHealthSnapshot:
        self._servers = await self._poll_servers()
        return await self._publish_current()

    async def run(self) -> None:
        interval = self.config.health_poll_interval_seconds
        refresh_task: asyncio.Task[tuple[ServerHealth, ...]] | None = (
            asyncio.create_task(self._poll_servers())
        )
        timer_task: asyncio.Future[None] | None = None
        ups_task = asyncio.create_task(self._ups_updated.wait())
        try:
            while True:
                active: set[asyncio.Future] = {ups_task}
                if refresh_task is not None:
                    active.add(refresh_task)
                if timer_task is not None:
                    active.add(timer_task)
                done, _pending = await asyncio.wait(
                    active,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                publish_needed = False

                if ups_task in done:
                    # Clear before publishing so an update that arrives during
                    # the Discord call remains set for the next loop.
                    self._ups_updated.clear()
                    ups_task = asyncio.create_task(self._ups_updated.wait())
                    publish_needed = True

                if refresh_task is not None and refresh_task in done:
                    self._servers = refresh_task.result()
                    refresh_task = None
                    timer_task = asyncio.ensure_future(self.sleep(interval))
                    publish_needed = True

                if timer_task is not None and timer_task in done:
                    timer_task = None
                    refresh_task = asyncio.create_task(self._poll_servers())

                if publish_needed:
                    await self._publish_current()
        finally:
            tasks = [
                task
                for task in (refresh_task, timer_task, ups_task)
                if task is not None and not task.done()
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_servers(self) -> tuple[ServerHealth, ...]:
        groups: dict[tuple[str, str], list[RemoteServer]] = {}
        for server in self.config.servers:
            groups.setdefault((server.agent_url, server.token), []).append(server)

        grouped_servers = tuple(groups.values())
        results = await asyncio.gather(
            *(self.agents.statuses(group[0]) for group in grouped_servers),
            return_exceptions=True,
        )

        states: dict[str, str] = {}
        for group, result in zip(grouped_servers, results, strict=True):
            if isinstance(result, BaseException):
                LOG.warning(
                    "Could not poll agent for %s: %s",
                    ", ".join(server.id for server in group),
                    result,
                )
                for server in group:
                    states[server.id] = "unreachable"
                continue

            for server in group:
                entry = result.get(server.id)
                state = entry.get("state") if isinstance(entry, dict) else None
                states[server.id] = (
                    state.strip().lower()
                    if isinstance(state, str) and state.strip()
                    else "unknown"
                )

        return tuple(
            ServerHealth(server.id, server.name, states.get(server.id, "unknown"))
            for server in self.config.servers
        )

    async def _publish_current(self) -> ControllerHealthSnapshot:
        snapshot = ControllerHealthSnapshot(
            level=assess_health(self._servers, self.config.ups.enabled, self._ups),
            servers=self._servers,
            ups=self._ups,
            observed_at=self.clock(),
        )
        self.latest_snapshot = snapshot
        try:
            await self.publisher.publish_health(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Could not publish controller health")
        return snapshot


__all__ = [
    "ControllerHealthMonitor",
    "ControllerHealthSnapshot",
    "HealthLevel",
    "HealthPublisher",
    "ServerHealth",
    "assess_health",
]

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from mc_manager.config import ControllerConfig, RemoteServer, UPSConfig
from mc_manager.health import (
    ControllerHealthMonitor,
    HealthLevel,
    ServerHealth,
    assess_health,
)
from mc_manager.ups import UPSReading


def make_config(*, ups_enabled: bool = False) -> ControllerConfig:
    return ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session-secret",
        cookie_secure=False,
        discord_token="discord-token",
        discord_guild_id=123,
        announcement_channel_id=456,
        servers=(
            RemoteServer(
                id="velocity",
                name="Velocity",
                agent_url="http://192.168.1.35:8766",
                token="host-two",
            ),
            RemoteServer(
                id="lobby",
                name="Lobby",
                agent_url="http://192.168.1.35:8766",
                token="host-two",
            ),
            RemoteServer(
                id="vanilla",
                name="Vanilla",
                agent_url="http://192.168.1.16:8766",
                token="host-one",
            ),
        ),
        ups=UPSConfig(enabled=ups_enabled),
        health_poll_interval_seconds=30,
    )


def reading(
    status: str = "OL",
    charge: float | None = 100,
    *,
    available: bool = True,
) -> UPSReading:
    return UPSReading(available, status, charge, 1_000)


def test_health_level_labels_and_icons() -> None:
    assert HealthLevel.ALL_GOOD.label == "All Good"
    assert HealthLevel.CAUTION.label == "Caution"
    assert HealthLevel.ATTENTION.label == "Attention"
    assert HealthLevel.ALL_GOOD.icon == "🟢"
    assert HealthLevel.CAUTION.icon == "🟡"
    assert HealthLevel.ATTENTION.icon == "🔴"


@pytest.mark.parametrize("state", ["unreachable", "unknown", "error"])
def test_server_unavailable_states_need_attention(state: str) -> None:
    assert assess_health((ServerHealth("one", "One", state),), False, None) is (
        HealthLevel.ATTENTION
    )


@pytest.mark.parametrize("state", ["failed", "starting", "stopping", "future-state"])
def test_unrecognized_server_states_need_attention(state: str) -> None:
    assert assess_health((ServerHealth("one", "One", state),), False, None) is (
        HealthLevel.ATTENTION
    )


@pytest.mark.parametrize("state", ["offline", "busy"])
def test_server_nonrunning_states_are_caution(state: str) -> None:
    assert assess_health((ServerHealth("one", "One", state),), False, None) is (
        HealthLevel.CAUTION
    )


@pytest.mark.parametrize("status", ["OB DISCHRG", "OL LB", "FSD", "OFF"])
def test_critical_ups_tokens_need_attention(status: str) -> None:
    assert assess_health((), True, reading(status)) is HealthLevel.ATTENTION


@pytest.mark.parametrize(
    "status",
    ["OL RB", "OL OVER", "BYPASS", "OL CAL", "OL DISCHRG", "OL TRIM", "OL BOOST"],
)
def test_ups_warning_tokens_are_caution(status: str) -> None:
    assert assess_health((), True, reading(status)) is HealthLevel.CAUTION


def test_ups_unavailable_is_attention_and_unknown_charge_is_caution() -> None:
    assert assess_health((), True, None) is HealthLevel.ATTENTION
    assert assess_health((), True, reading(available=False)) is HealthLevel.ATTENTION
    assert assess_health((), True, reading(charge=None)) is HealthLevel.CAUTION
    assert assess_health((), True, reading("vendor-garbage")) is (
        HealthLevel.ATTENTION
    )


def test_all_online_and_line_power_is_all_good() -> None:
    servers = (
        ServerHealth("velocity", "Velocity", "online"),
        ServerHealth("lobby", "Lobby", "online"),
    )
    assert assess_health(servers, True, reading("OL CHRG", 96)) is HealthLevel.ALL_GOOD


async def test_monitor_groups_shared_agents_and_maps_missing_entries() -> None:
    config = make_config()
    agents = Mock()

    async def statuses(server: RemoteServer) -> dict[str, dict]:
        if server.agent_url == "http://192.168.1.35:8766":
            return {"velocity": {"state": "online"}}
        return {"vanilla": {"state": "offline"}}

    agents.statuses = AsyncMock(side_effect=statuses)
    publisher = Mock()
    publisher.publish_health = AsyncMock()
    monitor = ControllerHealthMonitor(config, agents, publisher, clock=lambda: 42)

    snapshot = await monitor.poll_once()

    assert agents.statuses.await_count == 2
    assert [(server.id, server.state) for server in snapshot.servers] == [
        ("velocity", "online"),
        ("lobby", "unknown"),
        ("vanilla", "offline"),
    ]
    assert snapshot.level is HealthLevel.ATTENTION
    assert snapshot.observed_at == 42
    publisher.publish_health.assert_awaited_once_with(snapshot)


async def test_monitor_maps_agent_failure_to_unreachable() -> None:
    config = make_config()
    agents = Mock()

    async def statuses(server: RemoteServer) -> dict[str, dict]:
        if server.agent_url == "http://192.168.1.35:8766":
            raise RuntimeError("network down")
        return {"vanilla": {"state": "online"}}

    agents.statuses = AsyncMock(side_effect=statuses)
    publisher = Mock()
    publisher.publish_health = AsyncMock()
    monitor = ControllerHealthMonitor(config, agents, publisher)

    snapshot = await monitor.poll_once()

    assert [server.state for server in snapshot.servers] == [
        "unreachable",
        "unreachable",
        "online",
    ]
    assert snapshot.level is HealthLevel.ATTENTION


async def test_publisher_failure_does_not_escape_poll() -> None:
    config = make_config()
    agents = Mock()
    agents.statuses = AsyncMock(
        return_value={
            "velocity": {"state": "online"},
            "lobby": {"state": "online"},
            "vanilla": {"state": "online"},
        }
    )
    publisher = Mock()
    publisher.publish_health = AsyncMock(side_effect=RuntimeError("Discord down"))
    monitor = ControllerHealthMonitor(config, agents, publisher)

    snapshot = await monitor.poll_once()

    assert snapshot.level is HealthLevel.ALL_GOOD
    assert monitor.latest_snapshot is snapshot


async def test_run_publishes_ups_update_without_repolling_servers() -> None:
    config = make_config(ups_enabled=True)
    agents = Mock()
    agents.statuses = AsyncMock(
        side_effect=lambda server: {
            entry.id: {"state": "online"}
            for entry in config.servers
            if entry.agent_url == server.agent_url
        }
    )
    publisher = Mock()
    publisher.publish_health = AsyncMock()
    sleeping = asyncio.Event()
    never_release = asyncio.Event()

    async def sleeper(_seconds: float) -> None:
        sleeping.set()
        await never_release.wait()

    monitor = ControllerHealthMonitor(config, agents, publisher, sleeper=sleeper)
    monitor.update_ups(reading("OL", 100))
    task = asyncio.create_task(monitor.run())
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        initial_publications = publisher.publish_health.await_count
        assert initial_publications >= 1
        assert publisher.publish_health.await_args_list[-1].args[0].level is (
            HealthLevel.ALL_GOOD
        )
        initial_agent_polls = agents.statuses.await_count

        monitor.update_ups(reading("OB DISCHRG", 95))
        for _ in range(100):
            if publisher.publish_health.await_count > initial_publications:
                break
            await asyncio.sleep(0)

        assert publisher.publish_health.await_count == initial_publications + 1
        updated = publisher.publish_health.await_args_list[-1].args[0]
        assert updated.ups is not None
        assert updated.ups.status == "OB DISCHRG"
        assert updated.level is HealthLevel.ATTENTION
        assert agents.statuses.await_count == initial_agent_polls
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_ups_update_publishes_while_agent_refresh_is_still_running() -> None:
    config = make_config(ups_enabled=True)
    agents = Mock()
    release_agents = asyncio.Event()

    async def statuses(server: RemoteServer) -> dict[str, dict]:
        await release_agents.wait()
        return {
            entry.id: {"state": "online"}
            for entry in config.servers
            if entry.agent_url == server.agent_url
        }

    agents.statuses = AsyncMock(side_effect=statuses)
    publisher = Mock()
    publisher.publish_health = AsyncMock()
    monitor = ControllerHealthMonitor(config, agents, publisher)
    task = asyncio.create_task(monitor.run())
    try:
        for _ in range(100):
            if agents.statuses.await_count == 2:
                break
            await asyncio.sleep(0)
        assert agents.statuses.await_count == 2

        monitor.update_ups(reading("OB DISCHRG", 95))
        for _ in range(100):
            if publisher.publish_health.await_count:
                break
            await asyncio.sleep(0)

        assert publisher.publish_health.await_count == 1
        immediate = publisher.publish_health.await_args.args[0]
        assert immediate.level is HealthLevel.ATTENTION
        assert immediate.ups is not None
        assert immediate.ups.status == "OB DISCHRG"
        assert all(server.state == "unknown" for server in immediate.servers)

        release_agents.set()
        for _ in range(100):
            if publisher.publish_health.await_count >= 2:
                break
            await asyncio.sleep(0)
        assert publisher.publish_health.await_count == 2
        assert all(
            server.state == "online"
            for server in publisher.publish_health.await_args.args[0].servers
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_run_refreshes_servers_when_poll_interval_elapses() -> None:
    config = make_config()
    agents = Mock()
    agents.statuses = AsyncMock(
        side_effect=lambda server: {
            entry.id: {"state": "online"}
            for entry in config.servers
            if entry.agent_url == server.agent_url
        }
    )
    publisher = Mock()
    publisher.publish_health = AsyncMock()
    sleeping = asyncio.Event()
    release_tick = asyncio.Event()

    async def sleeper(_seconds: float) -> None:
        sleeping.set()
        await release_tick.wait()

    monitor = ControllerHealthMonitor(config, agents, publisher, sleeper=sleeper)
    task = asyncio.create_task(monitor.run())
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        assert publisher.publish_health.await_count == 1
        assert agents.statuses.await_count == 2

        release_tick.set()
        for _ in range(100):
            if publisher.publish_health.await_count >= 2:
                break
            await asyncio.sleep(0)

        assert publisher.publish_health.await_count == 2
        assert agents.statuses.await_count == 4
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

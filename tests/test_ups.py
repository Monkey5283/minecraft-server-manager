import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from mc_manager.config import ControllerConfig, RemoteServer, UPSConfig
from mc_manager.ups import (
    UPSMonitor,
    clean_upsc_value,
    parse_charge_percent,
    read_ups_reading,
    requires_protective_shutdown,
    ups_power_label,
    ups_status_message,
)


async def test_ups_monitor_stops_servers_and_runs_shutdown_script():
    server = RemoteServer(
        id="survival",
        name="Survival",
        agent_url="http://192.168.1.16:8766",
        token="secret",
    )
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session",
        cookie_secure=False,
        discord_token="discord",
        discord_guild_id=None,
        announcement_channel_id=123,
        servers=(server,),
        ups=UPSConfig(
            enabled=True,
            status_command=("/usr/bin/upsc", "cyberpower", "ups.status"),
            on_battery_delay_seconds=0,
            local_shutdown_delay_seconds=0,
            local_shutdown_command=("/usr/bin/systemctl", "poweroff"),
        ),
    )
    agents = AsyncMock()
    agents.action.return_value = {"id": "stop-job"}
    agents.script.return_value = {"id": "shutdown-job"}
    agents.statuses.return_value = {
        "survival": {"scripts": ["shutdown_host"]},
    }
    agents.job.side_effect = [
        {"state": "succeeded", "operation": "stop"},
        {"state": "succeeded", "operation": "script:shutdown_host"},
    ]
    commands: list[tuple[str, ...]] = []

    async def command_runner(command: tuple[str, ...]) -> str:
        commands.append(command)
        return ""

    monitor = UPSMonitor(
        config,
        agents,
        AsyncMock(),
        command_runner=command_runner,
        sleeper=AsyncMock(),
    )

    await monitor.handle_power_outage("OB DISCHRG")

    agents.action.assert_awaited_once_with(server, "stop")
    agents.script.assert_awaited_once_with(server, "shutdown_host")
    assert commands == [("/usr/bin/systemctl", "poweroff")]


async def test_ups_stops_all_services_before_one_shutdown_per_host():
    velocity = RemoteServer(
        id="velocity",
        name="Velocity",
        agent_url="http://192.168.1.35:8766",
        token="host-two",
    )
    lobby = RemoteServer(
        id="lobby",
        name="Lobby",
        agent_url="http://192.168.1.35:8766",
        token="host-two",
    )
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session",
        cookie_secure=False,
        discord_token="discord",
        discord_guild_id=None,
        announcement_channel_id=123,
        servers=(velocity, lobby),
        ups=UPSConfig(
            enabled=True,
            local_shutdown_delay_seconds=0,
            local_shutdown_command=("poweroff",),
        ),
    )
    agents = AsyncMock()
    agents.action.side_effect = lambda server, _action: {
        "id": f"stop-{server.id}"
    }
    lobby_stop_started = asyncio.Event()
    release_lobby_stop = asyncio.Event()

    async def job(_server: RemoteServer, job_id: str) -> dict:
        if job_id == "stop-lobby":
            lobby_stop_started.set()
            await release_lobby_stop.wait()
        return {
            "state": "succeeded",
            "operation": job_id,
        }

    agents.job.side_effect = job
    agents.statuses.return_value = {
        "velocity": {"scripts": []},
        "lobby": {"scripts": ["shutdown_host"]},
    }
    agents.script.return_value = {"id": "shutdown-host-two"}
    commands: list[tuple[str, ...]] = []

    async def command_runner(command: tuple[str, ...]) -> str:
        commands.append(command)
        return ""

    monitor = UPSMonitor(
        config,
        agents,
        AsyncMock(),
        command_runner=command_runner,
        sleeper=AsyncMock(),
    )

    task = asyncio.create_task(monitor.handle_power_outage("OB"))
    await asyncio.wait_for(lobby_stop_started.wait(), timeout=1)
    agents.statuses.assert_not_awaited()
    agents.script.assert_not_awaited()
    release_lobby_stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert agents.action.await_count == 2
    agents.statuses.assert_awaited_once_with(velocity)
    agents.script.assert_awaited_once_with(lobby, "shutdown_host")
    assert commands == [("poweroff",)]


async def test_ups_does_not_shutdown_shared_host_when_one_service_stop_fails():
    velocity = RemoteServer(
        id="velocity",
        name="Velocity",
        agent_url="http://192.168.1.35:8766",
        token="host-two",
    )
    lobby = RemoteServer(
        id="lobby",
        name="Lobby",
        agent_url="http://192.168.1.35:8766",
        token="host-two",
    )
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session",
        cookie_secure=False,
        discord_token="discord",
        discord_guild_id=None,
        announcement_channel_id=123,
        servers=(velocity, lobby),
        ups=UPSConfig(
            enabled=True,
            local_shutdown_delay_seconds=0,
            local_shutdown_command=("poweroff",),
        ),
    )
    agents = AsyncMock()
    agents.action.side_effect = lambda server, _action: {
        "id": f"stop-{server.id}"
    }

    async def job(_server: RemoteServer, job_id: str) -> dict:
        if job_id == "stop-velocity":
            return {"state": "failed", "error": "Velocity did not stop"}
        return {"state": "succeeded", "operation": job_id}

    agents.job.side_effect = job
    announce = AsyncMock()
    commands: list[tuple[str, ...]] = []

    async def command_runner(command: tuple[str, ...]) -> str:
        commands.append(command)
        return ""

    monitor = UPSMonitor(
        config,
        agents,
        announce,
        command_runner=command_runner,
        sleeper=AsyncMock(),
    )

    await monitor.handle_power_outage("OB")

    agents.statuses.assert_not_awaited()
    agents.script.assert_not_awaited()
    assert any(
        "Not shutting down the host" in call.args[0]
        for call in announce.await_args_list
    )
    assert commands == [("poweroff",)]


async def test_ups_monitor_announces_before_shutdown_delay():
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session",
        cookie_secure=False,
        discord_token="discord",
        discord_guild_id=None,
        announcement_channel_id=123,
        ups=UPSConfig(
            enabled=True,
            status_command=("status",),
            charge_command=("charge",),
            on_battery_delay_seconds=30,
            local_shutdown_delay_seconds=0,
            local_shutdown_command=("poweroff",),
        ),
    )
    status_responses = ["OB DISCHRG", "OB DISCHRG"]
    commands: list[tuple[str, ...]] = []

    async def command_runner(command: tuple[str, ...]) -> str:
        commands.append(command)
        if command == ("status",):
            return status_responses.pop(0)
        return ""

    announce = AsyncMock()
    status_sink = Mock()
    sleep_events: list[tuple[float, int]] = []

    async def sleeper(seconds: float) -> None:
        sleep_events.append((seconds, announce.await_count))

    monitor = UPSMonitor(
        config,
        AsyncMock(),
        announce,
        command_runner=command_runner,
        sleeper=sleeper,
        status_sink=status_sink,
    )

    await monitor.run()

    assert sleep_events[0] == (30, 1)
    first_message = announce.await_args_list[0].args[0]
    assert "Shutdown starts in 30 seconds unless power returns" in first_message
    assert "Stopping Minecraft servers now" in announce.await_args_list[1].args[0]
    assert status_sink.call_count == 2
    assert all(call.args[0].on_battery for call in status_sink.call_args_list)
    assert commands == [
        ("status",),
        ("charge",),
        ("status",),
        ("poweroff",),
    ]


async def test_ups_charge_updates_while_shutdown_delay_is_running():
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session",
        cookie_secure=False,
        discord_token="discord",
        discord_guild_id=None,
        announcement_channel_id=123,
        ups=UPSConfig(
            enabled=True,
            status_command=("status",),
            charge_command=("charge",),
            on_battery_delay_seconds=30,
            local_shutdown_delay_seconds=0,
            local_shutdown_command=("poweroff",),
        ),
    )
    statuses = ["OB DISCHRG", "OB DISCHRG"]

    async def command_runner(command: tuple[str, ...]) -> str:
        if command == ("status",):
            return statuses.pop(0)
        if command == ("charge",):
            return "73\n"
        return ""

    delay_started = asyncio.Event()
    release_delay = asyncio.Event()

    async def sleeper(seconds: float) -> None:
        if seconds == 30:
            delay_started.set()
            await release_delay.wait()

    announce = AsyncMock()
    status_sink = Mock()
    monitor = UPSMonitor(
        config,
        AsyncMock(),
        announce,
        command_runner=command_runner,
        sleeper=sleeper,
        status_sink=status_sink,
    )

    task = asyncio.create_task(monitor.run())
    await asyncio.wait_for(delay_started.wait(), timeout=1)
    for _ in range(100):
        if any(
            call.args[0].charge_percent == 73
            for call in status_sink.call_args_list
        ):
            break
        await asyncio.sleep(0)

    assert announce.await_count == 1
    assert any(
        call.args[0].charge_percent == 73
        for call in status_sink.call_args_list
    )

    release_delay.set()
    await asyncio.wait_for(task, timeout=1)


async def test_confirmation_failure_continues_fail_safe_shutdown():
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session",
        cookie_secure=False,
        discord_token="discord",
        discord_guild_id=None,
        announcement_channel_id=123,
        ups=UPSConfig(
            enabled=True,
            status_command=("status",),
            charge_command=("charge",),
            on_battery_delay_seconds=30,
            local_shutdown_delay_seconds=0,
            local_shutdown_command=("poweroff",),
        ),
    )
    commands: list[tuple[str, ...]] = []
    statuses: list[str | Exception] = [
        "OB DISCHRG",
        RuntimeError("NUT disconnected"),
    ]

    async def command_runner(command: tuple[str, ...]) -> str:
        commands.append(command)
        if command == ("status",):
            result = statuses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return ""

    announce = AsyncMock()
    status_sink = Mock()
    monitor = UPSMonitor(
        config,
        AsyncMock(),
        announce,
        command_runner=command_runner,
        sleeper=AsyncMock(),
        status_sink=status_sink,
    )

    await monitor.run()

    assert commands == [
        ("status",),
        ("charge",),
        ("status",),
        ("poweroff",),
    ]
    assert any(
        "Continuing the protective shutdown" in call.args[0]
        for call in announce.await_args_list
    )
    assert status_sink.call_args_list[-1].args[0].available is False


async def test_ups_monitor_cancels_shutdown_if_power_returns_during_delay():
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session",
        cookie_secure=False,
        discord_token="discord",
        discord_guild_id=None,
        announcement_channel_id=123,
        ups=UPSConfig(
            enabled=True,
            status_command=("status",),
            on_battery_delay_seconds=30,
            local_shutdown_delay_seconds=0,
            local_shutdown_command=("poweroff",),
        ),
    )

    async def command_runner(command: tuple[str, ...]) -> str:
        assert command == ("status",)
        return "OL CHRG"

    announce = AsyncMock()
    monitor = UPSMonitor(
        config,
        AsyncMock(),
        announce,
        command_runner=command_runner,
        sleeper=AsyncMock(),
    )

    result = await monitor.confirm_on_battery_after_delay("OB DISCHRG")

    assert result is None
    assert announce.await_count == 2
    assert (
        "Shutdown starts in 30 seconds unless power returns"
        in announce.await_args_list[0].args[0]
    )
    assert "Shutdown sequence canceled" in announce.await_args_list[1].args[0]


@pytest.mark.parametrize("status", ["FSD", "OFF", "BYPASS"])
async def test_ups_confirmation_only_cancels_for_explicit_line_power(status: str):
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session",
        cookie_secure=False,
        discord_token="discord",
        discord_guild_id=None,
        announcement_channel_id=123,
        ups=UPSConfig(
            enabled=True,
            status_command=("status",),
            charge_command=("charge",),
            on_battery_delay_seconds=30,
        ),
    )

    async def command_runner(command: tuple[str, ...]) -> str:
        if command == ("status",):
            return status
        return "80"

    announce = AsyncMock()
    monitor = UPSMonitor(
        config,
        AsyncMock(),
        announce,
        command_runner=command_runner,
        sleeper=AsyncMock(),
    )

    result = await monitor.confirm_on_battery_after_delay("OB DISCHRG")

    assert result == status
    assert not any(
        "Shutdown sequence canceled" in call.args[0]
        for call in announce.await_args_list
    )


def test_ups_status_detects_battery_states():
    assert UPSMonitor.is_on_battery("OB DISCHRG") is True
    assert UPSMonitor.is_on_battery("OL CHRG") is False
    assert UPSMonitor.is_on_battery("OL LB") is True
    assert requires_protective_shutdown("FSD") is True
    assert requires_protective_shutdown("OFF") is True
    assert requires_protective_shutdown("OL") is False


def test_clean_upsc_value_removes_ssl_warning():
    assert clean_upsc_value("Init SSL without certificate database\nOL\n") == "OL"
    assert clean_upsc_value("Init SSL without certificate database\n100\n") == "100"


def test_parse_charge_percent_validates_nut_output():
    assert parse_charge_percent("96\n") == 96
    assert parse_charge_percent("99.5\n") == 99.5
    assert parse_charge_percent("unknown\n") is None
    assert parse_charge_percent("101\n") is None


async def test_read_ups_reading_returns_structured_status_and_charge():
    ups = UPSConfig(status_command=("status",), charge_command=("charge",))

    async def command_runner(command: tuple[str, ...]) -> str:
        return {("status",): "OL CHRG\n", ("charge",): "87\n"}[command]

    reading = await read_ups_reading(ups, command_runner, clock=lambda: 1234)

    assert reading.available is True
    assert reading.status == "OL CHRG"
    assert reading.charge_percent == 87
    assert reading.observed_at == 1234
    assert reading.on_battery is False


async def test_empty_ups_status_is_unavailable_not_line_power():
    ups = UPSConfig(status_command=("status",), charge_command=("charge",))

    async def command_runner(command: tuple[str, ...]) -> str:
        assert command == ("status",)
        return "Init SSL without certificate database\n"

    reading = await read_ups_reading(ups, command_runner, clock=lambda: 1234)

    assert reading.available is False
    assert ups_power_label(reading) == "Unavailable"


async def test_unrecognized_ups_status_is_unavailable_not_line_power():
    ups = UPSConfig(status_command=("status",), charge_command=("charge",))

    async def command_runner(command: tuple[str, ...]) -> str:
        assert command == ("status",)
        return "vendor-garbage\n"

    reading = await read_ups_reading(ups, command_runner)

    assert reading.available is False
    assert ups_power_label(reading) == "Unavailable"


async def test_ups_status_message_includes_battery_charge():
    ups = UPSConfig(
        status_command=("/usr/bin/upsc", "cyberpower@localhost", "ups.status"),
        charge_command=("/usr/bin/upsc", "cyberpower@localhost", "battery.charge"),
    )
    responses = {
        ups.status_command: "OL\n",
        ups.charge_command: "96\n",
    }

    async def command_runner(command: tuple[str, ...]) -> str:
        return responses[command]

    message = await ups_status_message(ups, command_runner)

    assert "Online / line power" in message
    assert "96%" in message
    assert "Init SSL" not in message


async def test_ups_status_message_removes_ssl_warning():
    ups = UPSConfig(
        status_command=("/usr/bin/upsc", "cyberpower@localhost", "ups.status"),
        charge_command=("/usr/bin/upsc", "cyberpower@localhost", "battery.charge"),
    )
    responses = {
        ups.status_command: "Init SSL without certificate database\nOL\n",
        ups.charge_command: "Init SSL without certificate database\n100\n",
    }

    async def command_runner(command: tuple[str, ...]) -> str:
        return responses[command]

    message = await ups_status_message(ups, command_runner)

    assert "(`OL`)" in message
    assert "100%" in message
    assert "Init SSL" not in message

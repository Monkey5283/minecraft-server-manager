from unittest.mock import AsyncMock

from mc_manager.config import ControllerConfig, RemoteServer, UPSConfig
from mc_manager.ups import UPSMonitor, clean_upsc_value, ups_status_message


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
    agents.status.return_value = {"scripts": ["shutdown_host"]}
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


def test_ups_status_detects_battery_states():
    assert UPSMonitor.is_on_battery("OB DISCHRG") is True
    assert UPSMonitor.is_on_battery("OL CHRG") is False
    assert UPSMonitor.is_on_battery("OL LB") is True


def test_clean_upsc_value_removes_ssl_warning():
    assert clean_upsc_value("Init SSL without certificate database\nOL\n") == "OL"
    assert clean_upsc_value("Init SSL without certificate database\n100\n") == "100"


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

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from mc_manager.automation import RecoverySettings, ServerAutomation, due_restart
from mc_manager.client import AgentClient, action_origin
from mc_manager.config import RemoteServer
from mc_manager.panel import PanelStore, RestartSchedule
from mc_manager.ups import UPSReading


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc).timestamp()

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class FakeAgents:
    def __init__(self):
        self.states = {"survival": "online", "lobby": "online"}
        self.events = []
        self.fail_job = False

    async def status(self, server):
        return {"state": self.states[server.id], "console_enabled": True}

    async def console_command(self, server, command):
        self.events.append((server.id, "console", command))
        return {"accepted": True}

    async def action(self, server, action):
        self.events.append((server.id, action))
        if not self.fail_job:
            self.states[server.id] = "offline" if action == "stop" else "online"
        return {"id": "test-job"}

    async def job(self, server, job_id):
        return {"state": "failed" if self.fail_job else "succeeded", "error": "stop failed"}


@pytest.fixture
def automation(tmp_path):
    store = PanelStore(tmp_path / "panel.json", "admin", "password")
    agents = FakeAgents()
    clock = FakeClock()
    servers = {key: RemoteServer(key, key.title(), "http://agent", "secret") for key in agents.states}
    return ServerAutomation(store, agents, servers, AsyncMock(return_value=True), clock=clock, sleep=clock.sleep)


async def settle(auto):
    import asyncio
    await asyncio.gather(*auto.tasks.values())


def enable(auto, **kwargs):
    auto.store.put("monitoring", "survival", RecoverySettings(enabled=True, **kwargs).model_dump())


async def test_script_warns_then_stops_and_starts_before_discord(automation):
    auto = automation
    await auto.restart("survival", auto.clock() + 120, RestartSchedule(warning_seconds=120))
    events = auto.agents.events
    actions = [event[1] for event in events if len(event) == 2]
    assert actions == ["stop", "start"]
    assert "120 seconds" in events[0][2]
    assert any("in 60 seconds" in event[-1] for event in events)
    assert any("in 10 seconds" in event[-1] for event in events)
    assert ("survival", "console", "save-all flush") in events
    text = auto.announce.await_args.args[0]
    assert text.splitlines() == ["Survival", f"<t:{int(auto.clock())}:F>", "The Server has been Restarted."]
    assert auto.state("survival")["last_seen"] == "online"


async def test_failed_stop_never_starts_or_announces(automation):
    automation.agents.fail_job = True
    await automation.restart("survival", automation.clock(), RestartSchedule(warning_seconds=0))
    assert ("survival", "start") not in automation.agents.events
    automation.announce.assert_not_awaited()
    assert "stop failed" in automation.state("survival")["last_result"]


async def test_missing_warning_target_cancels_restart(automation):
    automation.agents.states["lobby"] = "offline"
    await automation.restart("survival", automation.clock(), RestartSchedule(warning_seconds=0, warning_servers=["lobby"]))
    assert not any(len(event) == 2 for event in automation.agents.events)
    automation.announce.assert_not_awaited()


async def test_recovery_waits_for_threshold_and_only_starts_offline_server(automation):
    auto = automation
    enable(auto)
    await auto.check_server("survival")
    auto.agents.states["survival"] = "offline"
    await auto.check_server("survival")
    assert not auto.tasks
    await auto.check_server("survival")
    await settle(auto)
    assert auto.agents.events == [("survival", "start")]
    auto.announce.assert_awaited_once()


async def test_existing_offline_server_is_not_automatically_started(automation):
    enable(automation, failure_threshold=1)
    automation.agents.states["survival"] = "offline"
    await automation.check_server("survival")
    assert not automation.tasks


@pytest.mark.parametrize("command", ["stop", "/stop", "end", "shutdown"])
async def test_intentional_console_stop_persists_across_controller_restart(automation, command):
    auto = automation
    enable(auto, failure_threshold=1)
    await auto.check_server("survival")
    auto.observe_operation(auto.servers["survival"], "POST", "/v1/servers/survival/console", {"command": command}, "admin")
    reloaded = PanelStore(auto.store.path, "admin", "password")
    assert reloaded.data["runtime"]["survival"]["intentional"]
    auto.agents.states["survival"] = "offline"
    await auto.check_server("survival")
    assert not auto.tasks


async def test_admin_stop_during_status_request_cannot_be_overwritten(automation):
    auto = automation
    enable(auto, failure_threshold=1)
    async def status(server):
        auto.observe_operation(server, "POST", "/v1/servers/survival/actions/stop", None, "admin")
        return {"state": "online", "console_enabled": True}
    auto.agents.status = status
    await auto.check_server("survival")
    assert auto.state("survival")["intentional"]
    assert auto.state("survival")["desired_running"] is False


async def test_unreachable_host_is_not_a_confirmed_crash(automation):
    enable(automation, failure_threshold=1)
    await automation.check_server("survival")
    automation.agents.status = AsyncMock(side_effect=RuntimeError("network disconnected"))
    await automation.check_server("survival")
    assert not automation.tasks
    assert automation.state("survival")["offline_checks"] == 0


async def test_recovery_cooldown_and_hourly_limit(automation):
    auto = automation
    enable(auto, failure_threshold=1, cooldown_seconds=60, max_restarts_per_hour=1)
    await auto.check_server("survival")
    auto.agents.states["survival"] = "offline"
    auto.agents.fail_job = True
    await auto.check_server("survival")
    await settle(auto)
    auto.clock.now += 120
    await auto.check_server("survival")
    assert auto.agents.events == [("survival", "start")]
    assert "limit" in auto.state("survival")["last_result"]


async def test_power_recovery_waits_for_confirmed_stable_power(automation):
    auto = automation
    enable(auto, failure_threshold=1)
    await auto.check_server("survival")
    auto.power_reading(UPSReading(True, "OB", 80, auto.clock()))
    auto.observe_operation(auto.servers["survival"], "POST", "/actions/stop", None, "power")
    auto.agents.states["survival"] = "offline"
    await auto.check_server("survival")
    assert not auto.tasks
    auto.power_reading(UPSReading(True, "OL", 80, auto.clock()))
    assert auto.blocked()
    auto.clock.now += 60
    auto.power_reading(UPSReading(True, "OL", 80, auto.clock()))
    await auto.check_server("survival")
    await settle(auto)
    assert auto.agents.events == [("survival", "start")]
    assert not auto.state("survival")["power_pending"]


async def test_power_recovery_can_be_disabled(automation):
    auto = automation
    enable(auto, failure_threshold=1, recover_after_power_loss=False)
    auto.record("survival", desired_running=True, power_pending=True)
    auto.agents.states["survival"] = "offline"
    await auto.check_server("survival")
    assert not auto.tasks


async def test_discord_failure_retries_notification_without_restart(automation):
    auto = automation
    auto.announce.return_value = False
    await auto.restart("survival", auto.clock(), RestartSchedule(warning_seconds=0))
    assert auto.state("survival")["discord_pending"]
    before = list(auto.agents.events)
    auto.clock.now += 60
    auto.announce.return_value = True
    await auto.send_notification("survival")
    assert auto.state("survival")["discord_pending"] is None
    assert auto.agents.events == before


async def test_schedule_claim_deduplicates(automation):
    auto = automation
    # Monday 04:00 Edmonton is 10:00 UTC in October.
    schedule = RestartSchedule(enabled=True, timezone="America/Edmonton", warning_seconds=0)
    auto.store.put("schedules", "survival", schedule.model_dump())
    await auto.tick()
    await settle(auto)
    await auto.tick()
    assert auto.agents.events.count(("survival", "stop")) == 1


def test_timezone_dst_and_cross_midnight_warning():
    schedule = RestartSchedule(enabled=True, timezone="America/Edmonton", times=["00:05"], warning_seconds=600).model_dump()
    due = due_restart(schedule, datetime(2026, 10, 12, 5, 55, tzinfo=timezone.utc))
    assert due[0] == "2026-10-12 00:05"
    schedule["times"] = ["02:30"]
    assert due_restart(schedule, datetime(2026, 3, 8, 9, 25, tzinfo=timezone.utc)) is None
    schedule["times"] = ["01:30"]
    assert due_restart(schedule, datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)) is None


def test_missed_warning_window_does_not_restart_without_notice():
    schedule = RestartSchedule(enabled=True, timezone="UTC", times=["04:00"], warning_seconds=300).model_dump()
    assert due_restart(schedule, datetime(2026, 10, 12, 4, 0, 20, tzinfo=timezone.utc)) is None


@pytest.mark.parametrize("settings", [{"check_interval_seconds": 1}, {"cooldown_seconds": 0}, {"max_restarts_per_hour": 0}])
def test_monitor_limits_are_validated(settings):
    with pytest.raises(ValidationError):
        RecoverySettings(**settings)


async def test_operation_origin_is_propagated_without_changing_agent_api(automation):
    client = AgentClient()
    seen = []
    client.operation_observer = lambda *args: seen.append(args[-1])
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "job"})))
    try:
        with action_origin("power"):
            await client.action(automation.servers["survival"], "stop")
        await client.action(automation.servers["survival"], "stop")
    finally:
        await client.close()
    assert seen == ["power", "admin"]


async def test_resume_interrupted_restart_starts_without_repeating_stop(automation):
    automation.agents.states["survival"] = "offline"
    automation.record("survival", desired_running=True)
    await automation.resume("survival", {"stage": "stopping", "notify": True})
    assert automation.agents.events == [("survival", "start")]
    automation.announce.assert_awaited_once()


async def test_admin_stop_cannot_be_cleared_by_stale_online_status(automation):
    auto = automation
    enable(auto)
    auto.record("survival", last_seen="offline", desired_running=False, intentional=True, intent_reason="admin_stop")
    await auto.check_server("survival")
    assert auto.state("survival")["intentional"] is True
    assert auto.state("survival")["desired_running"] is False


async def test_recovery_cooldown_prevents_rapid_retries(automation):
    auto = automation
    enable(auto, failure_threshold=1, cooldown_seconds=60, max_restarts_per_hour=3)
    await auto.check_server("survival")
    auto.agents.states["survival"] = "offline"
    auto.agents.fail_job = True
    await auto.check_server("survival")
    await settle(auto)
    await auto.check_server("survival")
    assert len(auto.agents.events) == 1
    auto.clock.now += 60
    await auto.check_server("survival")
    await settle(auto)
    assert len(auto.agents.events) == 2


async def test_admin_host_shutdown_marks_all_servers_intentional(automation):
    auto = automation
    auto.observe_operation(auto.servers["survival"], "POST", "/scripts/shutdown_host", None, "admin")
    assert all(auto.state(key)["intentional"] for key in auto.servers)


async def test_discord_toggle_suppresses_restart_alert(automation):
    await automation.restart("survival", automation.clock(), RestartSchedule(warning_seconds=0, discord_notify=False))
    automation.announce.assert_not_awaited()


async def test_readiness_check_delays_success_until_query_responds(automation):
    auto = automation
    original = auto.agents.status
    async def status(server):
        result = await original(server)
        return {**result, "player_tracking_available": True}
    auto.agents.status = status
    auto.agents.players = AsyncMock(side_effect=[RuntimeError("still booting"), (), ()])
    await auto.restart("survival", auto.clock(), RestartSchedule(warning_seconds=0))
    assert auto.agents.players.await_count == 3
    auto.announce.assert_awaited_once()


async def test_power_return_never_resurrects_intentionally_stopped_server(automation):
    auto = automation
    enable(auto, failure_threshold=1)
    auto.observe_operation(auto.servers["survival"], "POST", "/actions/stop", None, "admin")
    auto.observe_operation(auto.servers["survival"], "POST", "/actions/stop", None, "power")
    auto.agents.states["survival"] = "offline"
    await auto.check_server("survival")
    assert not auto.tasks


async def test_persisted_outage_blocks_recovery_after_controller_boot(automation):
    auto = automation
    auto.power_reading(UPSReading(True, "OB", 80, auto.clock()))
    restored = ServerAutomation(PanelStore(auto.store.path, "admin", "password"), auto.agents, auto.servers, auto.announce, ups_enabled=True, clock=auto.clock, sleep=auto.sleep)
    assert restored.blocked()
    restored.power_reading(UPSReading(True, "OL", 80, auto.clock()))
    assert restored.blocked()
    auto.clock.now += 60
    restored.power_reading(UPSReading(True, "OL", 80, auto.clock()))
    assert not restored.blocked()

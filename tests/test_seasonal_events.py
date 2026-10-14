import json
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from mc_manager.config import ControllerConfig, RemoteServer
from mc_manager.seasonal_events import SeasonalEventMonitor, current_event, event_message
from mc_manager.client import AgentUnavailable

NOW = 1797000000


def event(**changes):
    return dict(schema=1, active=True, updated_at=NOW, mode="auto", timezone="America/Edmonton",
                event_key="2026-12-01:christmas", theme="christmas", title="Monkey Merry Canopy",
                start=NOW-1000, end_exclusive=NOW+1000, **changes)


@pytest.mark.parametrize("changes", [{"active": False}, {"updated_at": NOW-301}, {"updated_at": NOW+61},
                                     {"end_exclusive": NOW}, {"start": NOW+1}, {"title": None}, {"schema": 2}])
def test_rejects_inactive_stale_or_invalid_status(changes):
    payload = event()
    payload.update(changes)
    assert current_event(json.dumps(payload), NOW) is None


def test_message_explains_gui_access_and_full_christmas_dates():
    message = event_message(event())
    assert "event board" in message and "parkour arch" in message
    assert "Christmas Eve, Christmas Day" in message and "New Year's Eve/Day" in message
    assert f"<t:{NOW+999}:D>" in message
    payload = event()
    payload.update(mode="christmas", start=None, end_exclusive=None)
    assert current_event(json.dumps(payload), NOW) is not None
    assert "enabled by the server team" in event_message(payload)


def make_monitor(tmp_path, monkeypatch):
    monkeypatch.setattr("mc_manager.seasonal_events.time.time", lambda: NOW)
    target = RemoteServer(id="lobby", name="Lobby", agent_url="http://agent", token="test")
    config = ControllerConfig(bind="127.0.0.1",port=8080,web_username="test",web_password="test",
                              session_secret="test",cookie_secure=False,discord_token="test",
                              discord_guild_id=123,announcement_channel_id=456,servers=(target,))
    bot = Mock()
    bot.servers = {"lobby": target}
    bot.is_ready.return_value = True
    bot.announce_seasonal = AsyncMock(return_value=True)
    agents = Mock()
    agents.status = AsyncMock(return_value={"state": "online"})
    agents.file_content = AsyncMock(return_value={"content": json.dumps(event())})
    return SeasonalEventMonitor(config, agents, bot, tmp_path/"events.json")


async def test_once_per_event_persists_across_restarts_and_catches_next_event(tmp_path, monkeypatch):
    monitor = make_monitor(tmp_path, monkeypatch)
    await monitor.poll()
    await monitor.poll()
    monitor.bot.announce_seasonal.assert_awaited_once()
    restarted = SeasonalEventMonitor(monitor.config, monitor.agents, monitor.bot, monitor.state_file)
    await restarted.poll()
    monitor.bot.announce_seasonal.assert_awaited_once()
    payload = event()
    payload.update(event_key="2027-01-02:new_year", theme="new_year", title="New Year in the Canopy")
    monitor.agents.file_content.return_value = {"content": json.dumps(payload)}
    await restarted.poll()
    assert monitor.bot.announce_seasonal.await_count == 2


async def test_failed_send_retries_without_claiming_delivery(tmp_path, monkeypatch):
    monitor = make_monitor(tmp_path, monkeypatch)
    monitor.bot.announce_seasonal.side_effect = [False, True]
    await monitor.poll()
    assert monitor.seen == [] and not monitor.state_file.exists()
    await monitor.poll()
    assert len(monitor.seen) == 1


async def test_offline_unready_missing_plugin_do_not_announce(tmp_path, monkeypatch):
    monitor = make_monitor(tmp_path, monkeypatch)
    monitor.bot.is_ready.return_value = False
    await monitor.poll()
    monitor.agents.status.assert_not_awaited()
    monitor.bot.is_ready.return_value = True
    monitor.agents.status.return_value = {"state": "offline"}
    await monitor.poll()
    monitor.agents.file_content.assert_not_awaited()
    monitor.agents.status.return_value = {"state": "online"}
    monitor.agents.file_content.side_effect = AgentUnavailable("missing", status_code=404)
    await monitor.poll()
    monitor.bot.announce_seasonal.assert_not_awaited()

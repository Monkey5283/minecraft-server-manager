from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from mc_manager.config import ControllerConfig, RemoteServer
from mc_manager.controller import create_controller_app
from mc_manager.panel import PanelStore, RestartSchedule


def config(tmp_path):
    return ControllerConfig(
        bind="127.0.0.1", port=8080, web_username="admin", web_password="old-password",
        session_secret="test-secret", cookie_secure=False, discord_token="unused",
        discord_guild_id=None, announcement_channel_id=None, health_presence_enabled=False,
        discovery_enabled=False, agent_registry_file=tmp_path / "paired.json",
        servers=(RemoteServer("survival", "Survival", "http://agent", "secret"),),
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr("mc_manager.controller.AgentClient", AsyncMock)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    return create_controller_app(config(tmp_path))


async def login(client, username="admin", password="old-password"):
    return await client.post("/api/login", json={"username": username, "password": password})


async def test_accounts_roles_passwords_and_persistence(app, tmp_path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin, httpx.AsyncClient(transport=transport, base_url="http://test") as operator:
        assert (await admin.get("/api/users")).status_code == 401
        assert (await admin.post("/api/manager/update")).status_code == 401
        assert (await login(admin)).status_code == 200
        assert (await admin.get("/api/session")).json()["admin"] is True
        payload = {"username": "operator", "password": "operator-password"}
        assert (await admin.post("/api/users", json=payload)).status_code == 201
        assert (await admin.post("/api/users", json=payload)).status_code == 409
        assert (await login(operator, **payload)).status_code == 200
        assert (await operator.get("/api/users")).status_code == 403
        assert (await operator.post("/api/users", json={**payload, "username": "intruder", "admin": True})).status_code == 403
        assert (await operator.post("/api/manager/update")).status_code == 403
        assert (await operator.get("/api/manager/update")).status_code == 403
        assert (await operator.put("/api/users/admin/password", json={"password": "hijacked-password"})).status_code == 403
        assert (await operator.put("/api/users/operator/password", json={"password": "new-password-long", "current_password": "wrong"})).status_code == 400
        assert (await admin.put("/api/users/operator/password", json={"password": "new-password-long"})).status_code == 200
        assert (await operator.get("/api/servers")).status_code == 401
        assert (await login(operator, "operator", "operator-password")).status_code == 401
        assert (await login(operator, "operator", "new-password-long")).status_code == 200
        assert (await admin.put("/api/users/admin/password", json={"password": "changed-admin-password", "current_password": "old-password"})).status_code == 200
        assert (await admin.get("/api/users")).status_code == 200
    stored = PanelStore(tmp_path / "panel-state.json", "admin", "old-password")
    assert stored.data == app.state.panel.data
    raw = (tmp_path / "panel-state.json").read_text()
    assert "changed-admin-password" not in raw and "new-password-long" not in raw


async def test_schedule_routes_validation_and_independence(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/servers/survival/restart-schedule"
        assert (await client.get(url)).status_code == 401
        assert (await client.put(url, json={})).status_code == 401
        await login(client)
        assert (await client.get(url)).json()["enabled"] is False
        payload = {"enabled": True, "days": [0, 2, 4], "times": ["04:00", "16:00"], "timezone": "America/Edmonton"}
        assert (await client.put(url, json=payload)).status_code == 200
        assert (await client.get(url)).json() == payload
        assert (await client.put(url, json={**payload, "times": ["25:00"]})).status_code == 422
        assert (await client.get("/api/servers/missing/restart-schedule")).status_code == 404


@pytest.mark.parametrize("payload", [{"times": []}, {"days": []}, {"days": [7]}, {"timezone": "../invalid"}, {"times": ["4:00"]}])
def test_schedule_validation(payload):
    with pytest.raises(ValidationError):
        RestartSchedule(**payload)


async def test_scheduler_deduplicates_and_persists_claims(tmp_path):
    path = tmp_path / "panel.json"
    store = PanelStore(path, "admin", "password")
    schedule = RestartSchedule(enabled=True, timezone="America/Edmonton", times=["04:00"]).model_dump()
    store.put("schedules", "survival", schedule)
    agents = AsyncMock()
    agents.status.return_value = {"state": "online"}
    agents.action.return_value = {"id": "job"}
    servers = {"survival": config(tmp_path).servers[0]}
    due = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
    await store.tick(agents, servers, due)
    await PanelStore(path, "admin", "password").tick(agents, servers, due)
    agents.action.assert_awaited_once_with(servers["survival"], "restart")
    assert "submitted" in store.data["schedules"]["survival"]["last_result"]


@pytest.mark.parametrize("state", ["offline", "busy", "unreachable"])
async def test_scheduler_skips_non_online(tmp_path, state):
    store = PanelStore(tmp_path / "panel.json", "admin", "password")
    store.put("schedules", "survival", RestartSchedule(enabled=True).model_dump())
    agents = AsyncMock()
    agents.status.return_value = {"state": state}
    await store.tick(agents, {"survival": config(tmp_path).servers[0]}, datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc))
    agents.action.assert_not_awaited()


async def test_scheduler_skips_disabled_missed_and_dst_fold(tmp_path):
    store = PanelStore(tmp_path / "panel.json", "admin", "password")
    store.put("schedules", "survival", RestartSchedule(enabled=True, timezone="America/Edmonton", times=["01:30"]).model_dump())
    agents = AsyncMock()
    servers = {"survival": config(tmp_path).servers[0]}
    await store.tick(agents, servers, datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc))
    await store.tick(agents, servers, datetime(2026, 11, 2, 8, 31, tzinfo=timezone.utc))
    store.put("schedules", "survival", RestartSchedule(enabled=False).model_dump())
    await store.tick(agents, servers, datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc))
    agents.status.assert_not_awaited()


async def test_scheduler_does_not_dispatch_if_claim_cannot_be_saved(tmp_path, monkeypatch):
    store = PanelStore(tmp_path / "panel.json", "admin", "password")
    store.put("schedules", "survival", RestartSchedule(enabled=True).model_dump())
    def fail(data):
        raise OSError("disk full")
    monkeypatch.setattr(store, "save", fail)
    agents = AsyncMock()
    with pytest.raises(OSError):
        await store.tick(agents, {"survival": config(tmp_path).servers[0]}, datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc))
    agents.action.assert_not_awaited()


async def test_update_request_and_result(tmp_path, monkeypatch):
    unit = tmp_path / "update.path"
    monkeypatch.setattr("mc_manager.controller.UPDATE_DIRECTORY", tmp_path)
    monkeypatch.setattr("mc_manager.controller.UPDATE_PATH_UNIT", unit)
    monkeypatch.setattr("mc_manager.controller.AgentClient", AsyncMock)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    app = create_controller_app(config(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        assert (await client.post("/api/manager/update")).status_code == 503
        unit.touch()
        assert (await client.post("/api/manager/update")).status_code == 202
        assert (tmp_path / "manager-update.request").read_text() == "update\n"
        assert (await client.post("/api/manager/update")).status_code == 409
        assert (await client.get("/api/manager/update")).json()["state"] == "running"
        (tmp_path / "manager-update-status.json").write_text('{"state":"failed"}')
        (tmp_path / "manager-update.request").unlink()
        assert (await client.get("/api/manager/update")).json()["state"] == "failed"


async def test_scheduler_failure_does_not_retry_same_minute(tmp_path):
    store = PanelStore(tmp_path / "panel.json", "admin", "password")
    store.put("schedules", "survival", RestartSchedule(enabled=True).model_dump())
    agents = AsyncMock()
    agents.status.side_effect = RuntimeError("agent unavailable")
    servers = {"survival": config(tmp_path).servers[0]}
    now = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)
    await store.tick(agents, servers, now)
    await store.tick(agents, servers, now)
    agents.status.assert_awaited_once()
    assert store.data["schedules"]["survival"]["last_result"].startswith("Failed:")


def test_update_service_is_independent_and_installed():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/mc-manager-update.service").read_text()
    assert "ExecStopPost=" in unit
    assert "PartOf=mc-manager-controller" not in unit
    for filename in ("bootstrap-minecraft-manager", "update-minecraft-manager"):
        script = (root / "deploy/scripts" / filename).read_text()
        assert "systemctl enable --now mc-manager-update.path" in script
        assert "run-minecraft-manager-web-update" in script

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
    agents = AsyncMock()
    agents.status.return_value = {"state": "online", "console_enabled": True}
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
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
        saved = (await client.get(url)).json()
        assert all(saved[key] == value for key, value in payload.items())
        assert (await client.put(url, json={**payload, "times": ["25:00"]})).status_code == 422
        assert (await client.get("/api/servers/missing/restart-schedule")).status_code == 404


async def test_timezone_dropdown_and_monitor_settings(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/servers/survival/monitoring"
        assert (await client.get("/api/timezones")).status_code == 401
        assert (await client.get(url)).status_code == 401
        assert (await client.put(url, json={"enabled": True})).status_code == 401
        await login(client)
        zones = (await client.get("/api/timezones")).json()
        assert len(zones) > 400
        assert {"America/Edmonton", "Asia/Tokyo", "Pacific/Auckland", "UTC"}.issubset(zones)
        assert (await client.get(url)).json()["settings"]["enabled"] is False
        assert (await client.put(url, json={"enabled": True, "check_interval_seconds": 20})).status_code == 200
        assert (await client.get(url)).json()["settings"]["check_interval_seconds"] == 20
        assert (await client.put(url, json={"check_interval_seconds": 0})).status_code == 422
        assert (await client.get("/api/servers/missing/monitoring")).status_code == 404
        assert (await client.post("/api/servers/survival/scheduled-restart")).status_code == 409


async def test_unknown_warning_destination_rejected(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.put("/api/servers/survival/restart-schedule", json={"enabled": True, "warning_servers": ["missing"]})
        assert response.status_code == 404


@pytest.mark.parametrize("payload", [{"times": []}, {"days": []}, {"days": [7]}, {"timezone": "../invalid"}, {"times": ["4:00"]}])
def test_schedule_validation(payload):
    with pytest.raises(ValidationError):
        RestartSchedule(**payload)


async def test_update_request_and_result(tmp_path, monkeypatch):
    unit = tmp_path / "update.path"
    monkeypatch.setattr("mc_manager.controller.UPDATE_DIRECTORY", tmp_path)
    monkeypatch.setattr("mc_manager.controller.UPDATE_PATH_UNIT", unit)
    agents = AsyncMock()
    agents.status.return_value = {"state": "online", "console_enabled": True}
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
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

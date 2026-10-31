from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from mc_manager.controller import create_controller_app
from mc_manager.config import ControllerConfig
from mc_manager.web_security import LoginThrottle


@pytest.fixture
def config(tmp_path):
    return ControllerConfig(bind="127.0.0.1", port=8080, web_username="admin", web_password="correct-password",
        session_secret="test-secret-that-is-not-production", cookie_secure=True, discord_token="unused",
        discord_guild_id=None, announcement_channel_id=None, discovery_enabled=False,
        agent_registry_file=tmp_path / "paired.json", launcher_download_url="https://launcher.example.net")


@pytest.fixture
def app(config, monkeypatch):
    monkeypatch.setattr("mc_manager.controller.AgentClient", AsyncMock)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    return create_controller_app(config)


def client(app, **kwargs):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://panel.private", **kwargs)


async def test_every_management_route_requires_a_valid_session(app):
    async with client(app, headers={"Cookie": "session=forged"}) as c:
        for route in app.routes:
            if not getattr(route, "path", "").startswith("/api/") or route.path in {"/api/login", "/api/session", "/api/logout"}:
                continue
            path = route.path
            for param in getattr(route, "param_convertors", {}):
                path = path.replace("{" + param + "}", "example")
            for method in route.methods - {"HEAD", "OPTIONS"}:
                response = await c.request(method, path)
                assert response.status_code == 401, (method, path, response.text)
        for path in ("/openapi.json", "/docs", "/redoc", "/launcher/v1/catalog"):
            assert (await c.get(path)).status_code == 404


async def test_browser_origin_boundary_and_cookie_flags(app):
    payload = {"username": "admin", "password": "correct-password"}
    async with client(app) as c:
        for headers in ({}, {"Origin": "null"}, {"Origin": "https://panel.private.evil.test"},
                        {"Origin": "https://launcher.example.net"}, {"X-Monkeycraft-Request": "1", "Sec-Fetch-Site": "same-site"}):
            assert (await c.post("/api/login", json=payload, headers=headers)).status_code == 403
        response = await c.post("/api/login", json=payload, headers={"Origin": "https://panel.private"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie and "domain=" not in cookie
        assert (await c.post("/api/logout")).status_code == 403
        assert (await c.get("/api/launcher")).json()["download_url"] == "https://launcher.example.net"
        assert (await c.post("/api/logout", headers={"X-Monkeycraft-Request": "1"})).status_code == 200
        assert (await c.get("/api/launcher")).status_code == 401


async def test_login_throttles_and_security_headers(app):
    async with client(app, headers={"Origin": "https://panel.private"}) as c:
        for _ in range(6):
            assert (await c.post("/api/login", json={"username": "admin", "password": "wrong"})).status_code == 401
        response = await c.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "max-age=" in response.headers["strict-transport-security"]
        assert "access-control-allow-origin" not in response.headers


def test_login_limits_expire_and_cover_password_spraying():
    now = [0.0]
    limit = LoginThrottle(clock=lambda: now[0])
    for i in range(15):
        limit.check("one-address", f"user-{i}")
    with pytest.raises(HTTPException) as exc:
        limit.check("one-address", "another-user")
    assert exc.value.status_code == 429
    now[0] = 301
    limit.check("one-address", "another-user")

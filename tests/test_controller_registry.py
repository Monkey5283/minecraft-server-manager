from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx

from mc_manager.config import ControllerConfig, RemoteServer
from mc_manager.controller import create_controller_app


def controller_config() -> ControllerConfig:
    return ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session-secret",
        cookie_secure=False,
        discord_token="discord-token",
        discord_guild_id=None,
        announcement_channel_id=None,
        servers=(
            RemoteServer(
                id="velocity",
                name="Velocity",
                agent_url="http://network-agent:8766",
                token="secret-agent-token",
            ),
        ),
        health_presence_enabled=False,
    )


def agent_entries() -> dict[str, dict]:
    return {
        "velocity": {
            "id": "velocity",
            "name": "Velocity",
            "state": "online",
            "actions": ["restart", "stop"],
            "scripts": [],
            "files_enabled": True,
            "player_tracking_available": False,
        },
        "creative": {
            "id": "creative",
            "name": "Creative World",
            "state": "offline",
            "actions": ["start"],
            "scripts": ["backup"],
            "files_enabled": True,
            "player_tracking_available": True,
        },
    }


def make_app(monkeypatch, registry_path: Path, entries: dict[str, dict] | None = None):
    entries = entries or agent_entries()
    agents = Mock()
    agents.statuses = AsyncMock(return_value=entries)
    agents.status = AsyncMock(side_effect=lambda server: dict(entries[server.id]))
    agents.close = AsyncMock()
    bot = Mock()
    bot.servers = {}
    bot.start = AsyncMock()
    bot.close = AsyncMock()
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: bot)
    app = create_controller_app(
        controller_config(), managed_servers_file=registry_path
    )
    return app, agents


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/login", json={"username": "admin", "password": "password"}
    )
    assert response.status_code == 200


async def test_registry_routes_require_login_and_discover_without_secrets(
    tmp_path: Path, monkeypatch
):
    app, agents = make_app(monkeypatch, tmp_path / "managed-servers.json")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/server-registry")).status_code == 401
        assert (await client.get("/api/server-registry/discover")).status_code == 401
        await login(client)

        discovery = await client.get("/api/server-registry/discover")

    assert discovery.status_code == 200
    assert [item["id"] for item in discovery.json()["candidates"]] == ["creative"]
    assert discovery.json()["candidates"][0]["source_server_id"] == "velocity"
    assert "secret-agent-token" not in discovery.text
    assert "agent_url" not in discovery.text
    agents.statuses.assert_awaited_once()


async def test_dashboard_assets_disable_stale_browser_caching(
    tmp_path: Path, monkeypatch
):
    app, _agents = make_app(monkeypatch, tmp_path / "managed-servers.json")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        index = await client.get("/")
        javascript = await client.get("/static/app.js")

    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-store"
    assert javascript.status_code == 200
    assert javascript.headers["cache-control"] == "no-store"


async def test_add_update_and_remove_server_without_restart(
    tmp_path: Path, monkeypatch
):
    registry_path = tmp_path / "managed-servers.json"
    app, _agents = make_app(monkeypatch, registry_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await login(client)
        added = await client.post(
            "/api/server-registry",
            json={
                "server_id": "creative",
                "source_server_id": "velocity",
                "name": "Creative World",
                "track_players": False,
            },
        )
        assert added.status_code == 200

        dashboard = await client.get("/api/servers")
        assert [server["controller_id"] for server in dashboard.json()] == [
            "velocity",
            "creative",
        ]
        assert dashboard.json()[1]["managed_registration"] is True

        updated = await client.put(
            "/api/server-registry/creative",
            json={"name": "Build Server", "track_players": True},
        )
        assert updated.status_code == 200
        configured = (await client.get("/api/server-registry")).json()["configured"]
        assert configured[1] == {
            "id": "creative",
            "name": "Build Server",
            "track_players": True,
            "managed": True,
            "source_server_id": "velocity",
        }

        bad_confirmation = await client.post(
            "/api/server-registry/creative/remove",
            json={"confirm_id": "wrong"},
        )
        assert bad_confirmation.status_code == 400
        static_remove = await client.post(
            "/api/server-registry/velocity/remove",
            json={"confirm_id": "velocity"},
        )
        assert static_remove.status_code == 400

        removed = await client.post(
            "/api/server-registry/creative/remove",
            json={"confirm_id": "creative"},
        )
        assert removed.status_code == 200
        assert [
            item["id"]
            for item in (await client.get("/api/server-registry")).json()["configured"]
        ] == ["velocity"]


async def test_guarded_delete_job_removes_dashboard_registration_after_success(
    tmp_path: Path, monkeypatch
):
    entries = agent_entries()
    entries["creative"]["deletion_enabled"] = True
    app, agents = make_app(
        monkeypatch, tmp_path / "managed-servers.json", entries
    )
    agents.delete_server = AsyncMock(
        return_value={
            "id": "delete-job",
            "server_id": "creative",
            "operation": "delete_server",
            "state": "queued",
        }
    )
    agents.job = AsyncMock(
        return_value={
            "id": "delete-job",
            "server_id": "creative",
            "operation": "delete_server",
            "state": "succeeded",
        }
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await login(client)
        await client.post(
            "/api/server-registry",
            json={
                "server_id": "creative",
                "source_server_id": "velocity",
                "name": "Creative World",
                "track_players": False,
            },
        )
        dashboard = await client.get("/api/servers")
        creative = next(
            item for item in dashboard.json() if item["controller_id"] == "creative"
        )
        bad_confirmation = await client.request(
            "DELETE",
            "/api/servers/creative",
            json={"confirmation": "wrong"},
        )
        protected_static = await client.request(
            "DELETE",
            "/api/servers/velocity",
            json={"confirmation": "velocity"},
        )
        deletion = await client.request(
            "DELETE",
            "/api/servers/creative",
            json={"confirmation": "creative"},
        )
        completed = await client.get(
            "/api/servers/creative/jobs/delete-job"
        )
        remaining = await client.get("/api/servers")

    assert creative["deletion_enabled"] is True
    assert bad_confirmation.status_code == 400
    assert protected_static.status_code == 409
    assert deletion.status_code == 200
    assert completed.json()["state"] == "succeeded"
    assert [item["controller_id"] for item in remaining.json()] == ["velocity"]
    agents.delete_server.assert_awaited_once()


async def test_registered_server_survives_app_recreation(tmp_path: Path, monkeypatch):
    registry_path = tmp_path / "managed-servers.json"
    app, _agents = make_app(monkeypatch, registry_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login(client)
        assert (
            await client.post(
                "/api/server-registry",
                json={
                    "server_id": "creative",
                    "source_server_id": "velocity",
                    "name": "Creative World",
                    "track_players": True,
                },
            )
        ).status_code == 200

    recreated, _agents = make_app(monkeypatch, registry_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recreated), base_url="http://test"
    ) as client:
        await login(client)
        configured = (await client.get("/api/server-registry")).json()["configured"]

    assert [server["id"] for server in configured] == ["velocity", "creative"]
    assert configured[1]["track_players"] is True


async def test_registration_confirms_agent_capabilities(tmp_path: Path, monkeypatch):
    entries = agent_entries()
    entries["creative"]["player_tracking_available"] = False
    app, _agents = make_app(
        monkeypatch, tmp_path / "managed-servers.json", entries
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await login(client)
        unavailable = await client.post(
            "/api/server-registry",
            json={
                "server_id": "creative",
                "source_server_id": "velocity",
                "name": "Creative World",
                "track_players": True,
            },
        )
        missing = await client.post(
            "/api/server-registry",
            json={
                "server_id": "invented",
                "source_server_id": "velocity",
                "name": "Invented",
            },
        )

    assert unavailable.status_code == 400
    assert "not configured" in unavailable.json()["detail"]
    assert missing.status_code == 404

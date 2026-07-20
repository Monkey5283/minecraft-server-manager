from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from mc_manager.config import ControllerConfig, RemoteServer
from mc_manager.controller import create_controller_app
from mc_manager.discovery import encode_beacon


def onboarding_config(tmp_path: Path) -> ControllerConfig:
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
        servers=(),
        health_presence_enabled=False,
        discovery_enabled=False,
        agent_registry_file=tmp_path / "paired-agents.json",
    )


async def test_dashboard_discovers_and_pairs_agent(tmp_path: Path, monkeypatch) -> None:
    agents = AsyncMock()
    agents.info.return_value = {
        "id": "paper-host",
        "name": "Paper Host",
        "servers": [{"id": "survival", "name": "Survival", "track_players": True}],
    }
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    app = create_controller_app(onboarding_config(tmp_path))
    app.state.discovery.observe(
        encode_beacon("paper-host", "Paper Host", 8766), "192.168.1.126"
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/agents/discovered")).status_code == 401
        await client.post("/api/login", json={"username": "admin", "password": "password"})
        discovered = (await client.get("/api/agents/discovered")).json()
        assert discovered[0]["url"] == "http://192.168.1.126:8766"

        paired = await client.post(
            "/api/agents/pair",
            json={"agent_id": "paper-host", "token": "agent-token"},
        )
        assert paired.status_code == 200
        listed = await client.get("/api/agents")
        assert listed.json()[0]["servers"][0]["id"] == "survival"

    persisted = json.loads((tmp_path / "paired-agents.json").read_text())
    assert persisted["agents"][0]["token"] == "agent-token"
    remote = agents.info.await_args.args[0]
    assert remote.agent_url == "http://192.168.1.126:8766"
    assert remote.token == "agent-token"


async def test_dashboard_adopts_configured_agent_without_exposing_token(
    tmp_path: Path, monkeypatch
) -> None:
    config = onboarding_config(tmp_path)
    config = replace(
        config,
        servers=(
            RemoteServer(
                id="vanillaplus",
                name="Vanilla Plus",
                agent_url="http://192.168.1.126:8766",
                token="existing-secret-token",
            ),
        ),
    )
    agents = AsyncMock()
    agents.info.return_value = {
        "id": "vanillaplus-host",
        "name": "Vanilla Plus Host",
        "provisioning_enabled": True,
        "servers": [
            {"id": "vanillaplus", "name": "Vanilla Plus", "track_players": False}
        ],
    }
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    app = create_controller_app(config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/login", json={"username": "admin", "password": "password"})
        configured = await client.get("/api/agents/configured")
        assert configured.status_code == 200
        assert configured.json() == [
            {
                "source_server_id": "vanillaplus",
                "name": "Vanilla Plus",
                "url": "http://192.168.1.126:8766",
                "paired": False,
            }
        ]
        assert "existing-secret-token" not in configured.text

        adopted = await client.post(
            "/api/agents/adopt", json={"source_server_id": "vanillaplus"}
        )
        assert adopted.status_code == 200
        listed = (await client.get("/api/agents")).json()
        assert listed[0]["id"] == "vanillaplus-host"
        assert listed[0]["servers"][0]["id"] == "vanillaplus"

    persisted = json.loads((tmp_path / "paired-agents.json").read_text())
    assert persisted["agents"][0]["token"] == "existing-secret-token"
    agents.info.assert_awaited_once()


async def test_configured_agent_must_support_provisioning(
    tmp_path: Path, monkeypatch
) -> None:
    config = onboarding_config(tmp_path)
    config = replace(
        config,
        servers=(
            RemoteServer(
                id="legacy",
                name="Legacy",
                agent_url="http://legacy:8766",
                token="legacy-token",
            ),
        ),
    )
    agents = AsyncMock()
    agents.info.return_value = {
        "id": "legacy-host",
        "name": "Legacy Host",
        "provisioning_enabled": False,
        "servers": [],
    }
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    app = create_controller_app(config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/login", json={"username": "admin", "password": "password"})
        response = await client.post(
            "/api/agents/adopt", json={"source_server_id": "legacy"}
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Agent provisioning is disabled"


async def test_controller_deletes_only_paired_dynamic_server(
    tmp_path: Path, monkeypatch
) -> None:
    agents = AsyncMock()
    agents.info.side_effect = [
        {
            "id": "paper-host",
            "name": "Paper Host",
            "provisioning_enabled": True,
            "servers": [
                {"id": "managed", "name": "Managed", "track_players": False}
            ],
        },
        {
            "id": "paper-host",
            "name": "Paper Host",
            "provisioning_enabled": True,
            "servers": [],
        },
    ]
    agents.status.return_value = {
        "id": "managed",
        "name": "Managed",
        "state": "online",
        "actions": ["start", "stop"],
        "scripts": ["backup"],
        "files_enabled": True,
        "console_enabled": True,
        "software_change_enabled": True,
        "server_delete_enabled": True,
    }
    agents.delete_server.return_value = {
        "id": "delete-job",
        "server_id": "managed",
        "operation": "delete_server",
    }
    agents.job.return_value = {
        "id": "delete-job",
        "server_id": "managed",
        "operation": "delete_server",
        "state": "succeeded",
        "output": '{"id":"managed","state":"deleted"}',
    }
    monkeypatch.setattr("mc_manager.controller.AgentClient", lambda: agents)
    monkeypatch.setattr("mc_manager.controller.MinecraftDiscordBot", lambda *a, **k: object())
    app = create_controller_app(onboarding_config(tmp_path))
    app.state.discovery.observe(
        encode_beacon("paper-host", "Paper Host", 8766), "192.168.1.126"
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/login", json={"username": "admin", "password": "password"})
        await client.post(
            "/api/agents/pair",
            json={"agent_id": "paper-host", "token": "agent-token"},
        )
        dashboard = (await client.get("/api/servers")).json()
        rejected = await client.post(
            "/api/servers/managed/delete",
            json={"confirm_id": "wrong", "delete_backups": False},
        )
        started = await client.post(
            "/api/servers/managed/delete",
            json={"confirm_id": "managed", "delete_backups": False},
        )
        completed = await client.get("/api/servers/managed/jobs/delete-job")
        remaining = (await client.get("/api/servers")).json()

    assert dashboard[0]["server_delete_enabled"] is True
    assert rejected.status_code == 400
    assert started.status_code == 200
    assert completed.status_code == 200
    assert remaining == []
    agents.delete_server.assert_awaited_once()

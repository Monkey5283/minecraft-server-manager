from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from mc_manager.config import ControllerConfig
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

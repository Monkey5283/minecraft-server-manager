import sys
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from mc_manager.agent import STANDARD_MINECRAFT_ROOT, Job, create_agent_app
from mc_manager.config import AgentConfig, AgentServer, PlayerQueryConfig
from mc_manager.minecraft_query import MinecraftQueryError


async def test_agent_requires_token_and_runs_only_configured_action(tmp_path: Path):
    ok_command = ((sys.executable, "-c", "print('started safely')"),)
    server = AgentServer(
        id="survival",
        name="Survival",
        working_directory=tmp_path,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": ok_command,
        },
        scripts={"backup": ok_command},
    )
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
        )
    )
    headers = {"Authorization": "Bearer test-token"}
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        assert (await client.get("/v1/servers")).status_code == 401
        status = await client.get("/v1/servers", headers=headers)
        assert status.status_code == 200
        assert status.json()[0]["state"] == "online"
        assert status.json()[0]["player_tracking_available"] is False

        forbidden = await client.post(
            "/v1/servers/survival/actions/destroy", headers=headers
        )
        assert forbidden.status_code == 404

        created = await client.post(
            "/v1/servers/survival/actions/start", headers=headers
        )
        assert created.status_code == 200
        job_id = created.json()["id"]

        for _ in range(50):
            job = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
            if job["state"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.02)

        assert job["state"] == "succeeded"
        assert "started safely" in job["output"]


async def test_agent_exposes_and_starts_managed_software_change(tmp_path: Path):
    ok_command = ((sys.executable, "-c", "print('online')"),)
    server_root = tmp_path / "vanillaplus"
    server_root.mkdir()
    managed_registry = tmp_path / "managed-servers.json"
    managed_registry.write_text(
        '{"version":1,"servers":[{"id":"vanillaplus","software":'
        '{"type":"paper","version":"1.21.11","java_path":"/usr/bin/java",'
        '"minimum_memory":"2G","maximum_memory":"6G"}}]}'
    )
    server = AgentServer(
        id="vanillaplus",
        name="Vanilla Plus",
        working_directory=server_root,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": ok_command,
        },
        scripts={},
    )
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
            provisioning_enabled=True,
            managed_servers_file=managed_registry,
        ),
        config_path=tmp_path / "agent.toml",
    )
    app.state.runtime.start_software_change_job = lambda selected, payload: Job(
        id="change-job",
        server_id=selected.id,
        operation="change_software",
    )
    app.state.runtime.start_delete_server_job = lambda selected, confirmation, **_: Job(
        id="delete-job",
        server_id=selected.id,
        operation="delete_server",
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer test-token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/v1/servers", headers=headers)
        app.state.runtime.maintenance_servers.add("vanillaplus")
        conflicting_action = await client.post(
            "/v1/servers/vanillaplus/actions/restart", headers=headers
        )
        app.state.runtime.maintenance_servers.discard("vanillaplus")
        changed = await client.post(
            "/v1/servers/vanillaplus/software",
            headers=headers,
            json={
                "type": "vanilla",
                "version": "1.21.11",
                "minimum_memory": "2G",
                "maximum_memory": "6G",
                "java_path": "/usr/bin/java",
                "accept_eula": True,
                "confirm_backup": True,
            },
        )
        rejected_delete = await client.request(
            "DELETE",
            "/v1/servers/vanillaplus",
            headers=headers,
            json={"confirmation": "wrong"},
        )
        deleted = await client.request(
            "DELETE",
            "/v1/servers/vanillaplus",
            headers=headers,
            json={"confirmation": "vanillaplus"},
        )

    assert listed.status_code == 200
    assert listed.json()[0]["software_change_enabled"] is True
    assert listed.json()[0]["deletion_enabled"] is True
    assert listed.json()[0]["software"]["type"] == "paper"
    assert conflicting_action.status_code == 409
    assert changed.status_code == 200
    assert changed.json()["id"] == "change-job"
    assert rejected_delete.status_code == 400
    assert deleted.status_code == 200
    assert deleted.json()["id"] == "delete-job"


async def test_agent_exposes_guarded_deletion_for_standard_legacy_server(
    tmp_path: Path,
):
    ok_command = ((sys.executable, "-c", "print('online')"),)
    service = "minecraft@vanillaplus.service"
    server = AgentServer(
        id="vanillaplus",
        name="Vanilla Plus",
        working_directory=(STANDARD_MINECRAFT_ROOT / "vanillaplus").resolve(),
        actions={
            "start": ok_command,
            "stop": (("sudo", "-n", "/usr/bin/systemctl", "stop", service),),
            "restart": ok_command,
            "status": ok_command,
        },
        scripts={},
    )
    managed_registry = tmp_path / "managed-servers.json"
    managed_registry.write_text('{"version":1,"servers":[]}')
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
            provisioning_enabled=True,
            managed_servers_file=managed_registry,
        ),
        config_path=tmp_path / "agent.toml",
    )
    captured: dict[str, bool] = {}

    def start_delete(selected, confirmation, *, legacy=False):
        captured["legacy"] = legacy
        return Job(
            id="legacy-delete-job",
            server_id=selected.id,
            operation="delete_server",
        )

    app.state.runtime.start_delete_server_job = start_delete
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer test-token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/v1/servers", headers=headers)
        deleted = await client.request(
            "DELETE",
            "/v1/servers/vanillaplus",
            headers=headers,
            json={"confirmation": "vanillaplus"},
        )

    assert listed.json()[0]["deletion_enabled"] is True
    assert deleted.status_code == 200
    assert captured == {"legacy": True}


async def test_agent_exposes_authenticated_player_snapshot(tmp_path: Path, monkeypatch):
    ok_command = ((sys.executable, "-c", "print('online')"),)
    server = AgentServer(
        id="lobby",
        name="Lobby",
        working_directory=tmp_path,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": ok_command,
        },
        scripts={},
        player_query=PlayerQueryConfig("127.0.0.1", 25566, 2),
    )
    query = AsyncMock(return_value=("Monkey5283", "Builder"))
    monkeypatch.setattr("mc_manager.agent.query_players", query)
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
        )
    )
    headers = {"Authorization": "Bearer test-token"}
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/v1/servers/lobby/players")).status_code == 401
        response = await client.get("/v1/servers/lobby/players", headers=headers)

    assert response.status_code == 200
    assert response.json()["players"] == ["Monkey5283", "Builder"]
    query.assert_awaited_once_with("127.0.0.1", 25566, 2)


async def test_agent_does_not_report_empty_when_query_fails_for_online_server(
    tmp_path: Path, monkeypatch
):
    ok_command = ((sys.executable, "-c", "print('online')"),)
    server = AgentServer(
        id="lobby",
        name="Lobby",
        working_directory=tmp_path,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": ok_command,
        },
        scripts={},
        player_query=PlayerQueryConfig("127.0.0.1", 25566),
    )
    monkeypatch.setattr(
        "mc_manager.agent.query_players",
        AsyncMock(side_effect=MinecraftQueryError("not responding")),
    )
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/servers/lobby/players",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 502
    assert "unavailable" in response.json()["detail"]


async def test_agent_rejects_player_snapshot_when_query_is_not_configured(
    tmp_path: Path
):
    ok_command = ((sys.executable, "-c", "print('online')"),)
    server = AgentServer(
        id="velocity",
        name="Velocity",
        working_directory=tmp_path,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": ok_command,
        },
        scripts={},
    )
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/servers/velocity/players",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 409


async def test_agent_reports_empty_players_only_for_configured_offline_exit_code(
    tmp_path: Path, monkeypatch
):
    ok_command = ((sys.executable, "-c", "print('ok')"),)
    offline_command = ((sys.executable, "-c", "raise SystemExit(3)"),)
    server = AgentServer(
        id="lobby",
        name="Lobby",
        working_directory=tmp_path,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": offline_command,
        },
        scripts={},
        player_query=PlayerQueryConfig("127.0.0.1", 25566),
    )
    monkeypatch.setattr(
        "mc_manager.agent.query_players",
        AsyncMock(side_effect=MinecraftQueryError("not responding")),
    )
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/servers/lobby/players",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200
    assert response.json()["state"] == "offline"
    assert response.json()["players"] == []


async def test_agent_keeps_unexpected_status_failure_unknown(tmp_path: Path, monkeypatch):
    ok_command = ((sys.executable, "-c", "print('ok')"),)
    broken_status = ((sys.executable, "-c", "raise SystemExit(1)"),)
    server = AgentServer(
        id="lobby",
        name="Lobby",
        working_directory=tmp_path,
        actions={
            "start": ok_command,
            "stop": ok_command,
            "restart": ok_command,
            "status": broken_status,
        },
        scripts={},
        player_query=PlayerQueryConfig("127.0.0.1", 25566),
    )
    monkeypatch.setattr(
        "mc_manager.agent.query_players",
        AsyncMock(side_effect=MinecraftQueryError("not responding")),
    )
    app = create_agent_app(
        AgentConfig(
            name="test-agent",
            bind="127.0.0.1",
            port=8766,
            token="test-token",
            servers=(server,),
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/servers/lobby/players",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 502
    assert "failed unexpectedly" in response.json()["detail"]

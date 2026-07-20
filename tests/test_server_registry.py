import json
from pathlib import Path

import pytest

from mc_manager.config import RemoteServer
from mc_manager.server_registry import (
    ManagedServer,
    ManagedServerRegistry,
    ServerRegistryError,
)


def base_servers() -> tuple[RemoteServer, ...]:
    return (
        RemoteServer(
            id="velocity",
            name="Velocity",
            agent_url="http://network-agent:8766",
            token="network-token",
        ),
    )


def test_registry_persists_and_materializes_agent_credentials(tmp_path: Path):
    path = tmp_path / "managed-servers.json"
    registry = ManagedServerRegistry(path, base_servers())
    registry.load()

    created = registry.add(
        ManagedServer(
            id="lobby-two",
            name="Lobby Two",
            source_server_id="velocity",
            track_players=True,
        )
    )

    assert created == RemoteServer(
        id="lobby-two",
        name="Lobby Two",
        agent_url="http://network-agent:8766",
        token="network-token",
        track_players=True,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "servers": [
            {
                "id": "lobby-two",
                "name": "Lobby Two",
                "source_server_id": "velocity",
                "track_players": True,
            }
        ],
    }

    reloaded = ManagedServerRegistry(path, base_servers())
    reloaded.load()
    assert reloaded.materialized() == (created,)


def test_registry_updates_and_removes_only_managed_entries(tmp_path: Path):
    path = tmp_path / "managed-servers.json"
    registry = ManagedServerRegistry(path, base_servers())
    registry.load()
    registry.add(ManagedServer("creative", "Creative", "velocity"))

    updated = registry.update("creative", name="  Creative Plus  ", track_players=True)

    assert updated.name == "Creative Plus"
    assert updated.track_players is True
    assert registry.remove("creative").id == "creative"
    assert json.loads(path.read_text(encoding="utf-8"))["servers"] == []
    with pytest.raises(ServerRegistryError, match="dashboard-added"):
        registry.remove("velocity")


def test_registry_suppresses_legacy_base_server_persistently(tmp_path: Path):
    path = tmp_path / "managed-servers.json"
    registry = ManagedServerRegistry(path, base_servers())
    registry.load()

    registry.suppress_base("velocity")

    assert registry.active_base_servers() == ()
    assert json.loads(path.read_text(encoding="utf-8"))[
        "deleted_legacy_servers"
    ] == ["velocity"]
    reloaded = ManagedServerRegistry(path, base_servers())
    reloaded.load()
    assert reloaded.active_base_servers() == ()


@pytest.mark.parametrize("server_id", ["", "UPPERCASE", "has spaces", "../escape"])
def test_registry_rejects_unsafe_server_ids(tmp_path: Path, server_id: str):
    registry = ManagedServerRegistry(tmp_path / "managed-servers.json", base_servers())
    registry.load()

    with pytest.raises(ServerRegistryError, match="Server id"):
        registry.add(ManagedServer(server_id, "Example", "velocity"))


def test_registry_rejects_base_collisions_and_unknown_sources(tmp_path: Path):
    registry = ManagedServerRegistry(tmp_path / "managed-servers.json", base_servers())
    registry.load()

    with pytest.raises(ServerRegistryError, match="already registered"):
        registry.add(ManagedServer("velocity", "Duplicate", "velocity"))
    with pytest.raises(ServerRegistryError, match="Credential source"):
        registry.add(ManagedServer("creative", "Creative", "missing"))


def test_registry_rejects_invalid_persisted_data(tmp_path: Path):
    path = tmp_path / "managed-servers.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": [
                    {
                        "id": "creative",
                        "name": "Creative",
                        "source_server_id": "missing",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ServerRegistryError, match="unknown credential source"):
        ManagedServerRegistry(path, base_servers()).load()

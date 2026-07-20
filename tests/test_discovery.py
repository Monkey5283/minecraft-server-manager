from __future__ import annotations

import json
from pathlib import Path

import pytest

from mc_manager.agent_registry import PairedAgent, PairedAgentStore, RegisteredServer
from mc_manager.discovery import DiscoveryRegistry, encode_beacon, parse_beacon


def test_discovery_beacon_contains_identity_but_no_secret() -> None:
    beacon = encode_beacon("host-one", "Host One", 8766)
    payload = json.loads(beacon)

    assert payload["id"] == "host-one"
    assert "token" not in payload
    discovered = parse_beacon(beacon, "192.168.1.126", now=100)
    assert discovered.url == "http://192.168.1.126:8766"


def test_discovery_registry_expires_stale_agents() -> None:
    registry = DiscoveryRegistry(ttl_seconds=30)
    registry.observe(encode_beacon("one", "One", 8766), "10.0.0.2", now=100)

    assert len(registry.list(now=129)) == 1
    assert registry.list(now=131) == []


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"{}", encode_beacon("one", "One", 8766) + b"x" * 3000],
)
def test_discovery_rejects_invalid_packets(payload: bytes) -> None:
    with pytest.raises(ValueError):
        parse_beacon(payload, "10.0.0.2")


def test_paired_agent_store_round_trips_protected_registry(tmp_path: Path) -> None:
    path = tmp_path / "paired.json"
    store = PairedAgentStore(path)
    store.put(
        PairedAgent(
            id="host-one",
            name="Host One",
            url="http://10.0.0.2:8766",
            token="secret",
            servers=[RegisteredServer("paper", "Paper", True)],
        )
    )

    loaded = PairedAgentStore(path).get("host-one")
    assert loaded is not None
    assert loaded.token == "secret"
    assert loaded.servers[0].track_players is True

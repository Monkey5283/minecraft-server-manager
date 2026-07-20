from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from dataclasses import asdict, dataclass
from typing import Any


LOG = logging.getLogger("mc_manager.discovery")
DISCOVERY_MAGIC = "minecraft-manager-agent"
DISCOVERY_VERSION = 1
DEFAULT_DISCOVERY_PORT = 8765
MAX_BEACON_BYTES = 2048


@dataclass(frozen=True)
class DiscoveredAgent:
    id: str
    name: str
    address: str
    port: int
    last_seen: float

    @property
    def url(self) -> str:
        host = f"[{self.address}]" if ":" in self.address else self.address
        return f"http://{host}:{self.port}"

    def as_public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["url"] = self.url
        return result


def encode_beacon(agent_id: str, name: str, port: int) -> bytes:
    payload = {
        "magic": DISCOVERY_MAGIC,
        "version": DISCOVERY_VERSION,
        "id": agent_id,
        "name": name,
        "port": port,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def parse_beacon(data: bytes, address: str, *, now: float | None = None) -> DiscoveredAgent:
    if len(data) > MAX_BEACON_BYTES:
        raise ValueError("discovery beacon is too large")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid discovery beacon") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid discovery beacon")
    if payload.get("magic") != DISCOVERY_MAGIC or payload.get("version") != DISCOVERY_VERSION:
        raise ValueError("unsupported discovery beacon")
    agent_id = payload.get("id")
    name = payload.get("name")
    port = payload.get("port")
    if not isinstance(agent_id, str) or not agent_id or len(agent_id) > 128:
        raise ValueError("invalid discovery agent id")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValueError("invalid discovery agent name")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("invalid discovery agent port")
    return DiscoveredAgent(agent_id, name.strip(), address, port, now or time.time())


class DiscoveryRegistry:
    def __init__(self, ttl_seconds: float = 45.0):
        self.ttl_seconds = ttl_seconds
        self._agents: dict[str, DiscoveredAgent] = {}

    def observe(self, data: bytes, address: str, *, now: float | None = None) -> None:
        agent = parse_beacon(data, address, now=now)
        self._agents[agent.id] = agent

    def list(self, *, now: float | None = None) -> list[DiscoveredAgent]:
        current = now or time.time()
        self._agents = {
            key: value
            for key, value in self._agents.items()
            if current - value.last_seen <= self.ttl_seconds
        }
        return sorted(self._agents.values(), key=lambda item: (item.name.lower(), item.id))

    def get(self, agent_id: str) -> DiscoveredAgent | None:
        self.list()
        return self._agents.get(agent_id)


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, registry: DiscoveryRegistry):
        self.registry = registry

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            self.registry.observe(data, addr[0])
        except ValueError:
            LOG.debug("Ignored invalid discovery packet from %s", addr[0])


async def listen_for_agents(
    registry: DiscoveryRegistry,
    port: int = DEFAULT_DISCOVERY_PORT,
) -> None:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DiscoveryProtocol(registry),
        local_addr=("0.0.0.0", port),
        allow_broadcast=True,
    )
    try:
        await asyncio.Future()
    finally:
        transport.close()


async def advertise_agent(
    agent_id: str,
    name: str,
    agent_port: int,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
    interval_seconds: float = 10.0,
) -> None:
    payload = encode_beacon(agent_id, name, agent_port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                await loop.sock_sendto(sock, payload, ("255.255.255.255", discovery_port))
            except OSError:
                LOG.exception("Could not send LAN discovery beacon")
            await asyncio.sleep(interval_seconds)
    finally:
        sock.close()

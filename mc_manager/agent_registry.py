from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RegisteredServer:
    id: str
    name: str
    track_players: bool = False


@dataclass
class PairedAgent:
    id: str
    name: str
    url: str
    token: str
    servers: list[RegisteredServer] = field(default_factory=list)


class PairedAgentStore:
    def __init__(self, path: Path):
        self.path = path
        self.agents: dict[str, PairedAgent] = {}
        self.load()

    def load(self) -> None:
        self.agents = {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        for raw in payload.get("agents", []) if isinstance(payload, dict) else []:
            try:
                servers = [
                    RegisteredServer(
                        id=str(item["id"]),
                        name=str(item.get("name", item["id"])),
                        track_players=bool(item.get("track_players", False)),
                    )
                    for item in raw.get("servers", [])
                ]
                agent = PairedAgent(
                    id=str(raw["id"]),
                    name=str(raw["name"]),
                    url=str(raw["url"]).rstrip("/"),
                    token=str(raw["token"]),
                    servers=servers,
                )
            except (KeyError, TypeError, ValueError):
                continue
            if agent.id and agent.token and agent.url.startswith(("http://", "https://")):
                self.agents[agent.id] = agent

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload: dict[str, Any] = {
            "version": 1,
            "agents": [asdict(agent) for agent in self.agents.values()],
        }
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def put(self, agent: PairedAgent) -> None:
        self.agents[agent.id] = agent
        self.save()

    def get(self, agent_id: str) -> PairedAgent | None:
        return self.agents.get(agent_id)

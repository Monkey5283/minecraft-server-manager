from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import RemoteServer


SERVER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SERVER_NAME_LENGTH = 80


class ServerRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ManagedServer:
    id: str
    name: str
    source_server_id: str
    track_players: bool = False


def validate_server_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ServerRegistryError("Server name must not be empty")
    if len(name) > MAX_SERVER_NAME_LENGTH:
        raise ServerRegistryError(
            f"Server name must be at most {MAX_SERVER_NAME_LENGTH} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ServerRegistryError("Server name must not contain control characters")
    return name


class ManagedServerRegistry:
    def __init__(self, path: Path, base_servers: tuple[RemoteServer, ...]):
        self.path = path
        self.base_servers = {server.id: server for server in base_servers}
        self.entries: dict[str, ManagedServer] = {}
        self.suppressed_base_ids: set[str] = set()

    def load(self) -> tuple[ManagedServer, ...]:
        if not self.path.exists():
            self.entries = {}
            self.suppressed_base_ids = set()
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ServerRegistryError(
                f"Could not read managed server registry: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ServerRegistryError("Managed server registry has an invalid version")
        raw_entries = payload.get("servers")
        if not isinstance(raw_entries, list):
            raise ServerRegistryError("Managed server registry must contain a server list")
        raw_suppressed = payload.get("deleted_legacy_servers", [])
        if not isinstance(raw_suppressed, list) or not all(
            isinstance(server_id, str) and server_id in self.base_servers
            for server_id in raw_suppressed
        ):
            raise ServerRegistryError(
                "Managed server registry has invalid deleted legacy server ids"
            )

        loaded: dict[str, ManagedServer] = {}
        for index, raw in enumerate(raw_entries):
            if not isinstance(raw, dict):
                raise ServerRegistryError(f"Managed server entry {index} is invalid")
            server_id = raw.get("id")
            if not isinstance(server_id, str) or not SERVER_ID_PATTERN.fullmatch(server_id):
                raise ServerRegistryError(f"Managed server entry {index} has an invalid id")
            if server_id in self.base_servers or server_id in loaded:
                raise ServerRegistryError(f"Duplicate managed server id: {server_id}")
            source_server_id = raw.get("source_server_id")
            if not isinstance(source_server_id, str):
                raise ServerRegistryError(
                    f"Managed server {server_id} has an invalid credential source"
                )
            if source_server_id not in self.base_servers:
                raise ServerRegistryError(
                    f"Managed server {server_id} references an unknown credential source"
                )
            name = raw.get("name")
            if not isinstance(name, str):
                raise ServerRegistryError(
                    f"Managed server {server_id} has an invalid name"
                )
            track_players = raw.get("track_players", False)
            if not isinstance(track_players, bool):
                raise ServerRegistryError(
                    f"Managed server {server_id} has an invalid player tracking value"
                )
            loaded[server_id] = ManagedServer(
                id=server_id,
                name=validate_server_name(name),
                source_server_id=source_server_id,
                track_players=track_players,
            )
        self.entries = loaded
        self.suppressed_base_ids = set(raw_suppressed)
        return tuple(loaded.values())

    def active_base_servers(self) -> tuple[RemoteServer, ...]:
        return tuple(
            server
            for server_id, server in self.base_servers.items()
            if server_id not in self.suppressed_base_ids
        )

    def materialize(self, entry: ManagedServer) -> RemoteServer:
        source = self.base_servers[entry.source_server_id]
        return RemoteServer(
            id=entry.id,
            name=entry.name,
            agent_url=source.agent_url,
            token=source.token,
            track_players=entry.track_players,
        )

    def materialized(self) -> tuple[RemoteServer, ...]:
        return tuple(self.materialize(entry) for entry in self.entries.values())

    def add(self, entry: ManagedServer) -> RemoteServer:
        if entry.id in self.base_servers or entry.id in self.entries:
            raise ServerRegistryError(f"Server id is already registered: {entry.id}")
        if not SERVER_ID_PATTERN.fullmatch(entry.id):
            raise ServerRegistryError(
                "Server id must contain lowercase letters, numbers, '-' or '_'"
            )
        if entry.source_server_id not in self.base_servers:
            raise ServerRegistryError("Credential source is not configured")
        normalized = ManagedServer(
            id=entry.id,
            name=validate_server_name(entry.name),
            source_server_id=entry.source_server_id,
            track_players=entry.track_players,
        )
        self.entries[normalized.id] = normalized
        try:
            self._save()
        except Exception:
            del self.entries[normalized.id]
            raise
        return self.materialize(normalized)

    def update(self, server_id: str, *, name: str, track_players: bool) -> RemoteServer:
        current = self.entries.get(server_id)
        if current is None:
            raise ServerRegistryError("Only dashboard-added servers can be edited")
        updated = ManagedServer(
            id=current.id,
            name=validate_server_name(name),
            source_server_id=current.source_server_id,
            track_players=track_players,
        )
        self.entries[server_id] = updated
        try:
            self._save()
        except Exception:
            self.entries[server_id] = current
            raise
        return self.materialize(updated)

    def remove(self, server_id: str) -> ManagedServer:
        current = self.entries.get(server_id)
        if current is None:
            raise ServerRegistryError("Only dashboard-added servers can be removed")
        del self.entries[server_id]
        try:
            self._save()
        except Exception:
            self.entries[server_id] = current
            raise
        return current

    def suppress_base(self, server_id: str) -> None:
        if server_id not in self.base_servers:
            raise ServerRegistryError("Only controller.toml servers can be suppressed")
        if server_id in self.suppressed_base_ids:
            return
        self.suppressed_base_ids.add(server_id)
        try:
            self._save()
        except Exception:
            self.suppressed_base_ids.remove(server_id)
            raise

    def public_entries(self) -> list[dict]:
        return [asdict(entry) for entry in self.entries.values()]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        payload = {
            "version": 1,
            "servers": [asdict(entry) for entry in self.entries.values()],
        }
        if self.suppressed_base_ids:
            payload["deleted_legacy_servers"] = sorted(self.suppressed_base_ids)
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".managed-servers-", dir=self.path.parent
            )
            temporary_path = Path(raw_path)
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                descriptor = -1
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError as exc:
            raise ServerRegistryError(f"Could not save managed server registry: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


__all__ = [
    "ManagedServer",
    "ManagedServerRegistry",
    "ServerRegistryError",
    "validate_server_name",
]

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .client import AgentClient
from .config import ControllerConfig, RemoteServer


LOG = logging.getLogger("mc_manager.players")
MAX_FINALIZE_FAILURES = 3


class PlayerSessionMessenger(Protocol):
    async def start_player_session(
        self,
        channel_id: int,
        player_name: str,
        server_name: str,
        started_at: float,
    ) -> int | None: ...

    async def update_player_session(
        self,
        channel_id: int,
        message_id: int,
        player_name: str,
        server_name: str,
        started_at: float,
    ) -> int | None: ...

    async def finish_player_session(
        self,
        channel_id: int,
        message_id: int,
        player_name: str,
        server_name: str,
        started_at: float,
        ended_at: float,
    ) -> int | None: ...


@dataclass
class PlayerSession:
    normalized_name: str
    display_name: str
    server_id: str
    server_name: str
    channel_id: int
    message_id: int
    started_at: float
    missing_since: float | None = None
    finalize_failures: int = 0


class PlayerPresenceMonitor:
    def __init__(
        self,
        config: ControllerConfig,
        agents: AgentClient,
        messenger: PlayerSessionMessenger,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        state_file: Path | None = None,
    ) -> None:
        self.config = config
        self.tracking = config.player_tracking
        self.servers = tuple(server for server in config.servers if server.track_players)
        self.agents = agents
        self.messenger = messenger
        self.clock = clock
        self.sleep = sleep
        self.state_file = state_file or self.tracking.state_file
        self.sessions: dict[str, PlayerSession] = {}
        self._failed_servers: set[str] = set()
        self._load_state()

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Unexpected player presence monitor error")
            await self.sleep(self.tracking.poll_interval_seconds)

    async def poll_once(self) -> None:
        snapshots = await asyncio.gather(
            *(self.agents.players(server) for server in self.servers),
            return_exceptions=True,
        )
        all_healthy = True
        observed: dict[str, list[tuple[str, RemoteServer]]] = {}

        for server, snapshot in zip(self.servers, snapshots, strict=True):
            if isinstance(snapshot, BaseException):
                all_healthy = False
                if server.id not in self._failed_servers:
                    LOG.warning("Player snapshot unavailable for %s: %s", server.id, snapshot)
                    self._failed_servers.add(server.id)
                continue
            if server.id in self._failed_servers:
                LOG.info("Player snapshots recovered for %s", server.id)
                self._failed_servers.remove(server.id)
            for raw_name in snapshot:
                player_name = raw_name.strip()
                if not player_name or len(player_name) > 64:
                    LOG.warning("Ignoring invalid player name from %s", server.id)
                    continue
                observed.setdefault(player_name.casefold(), []).append(
                    (player_name, server)
                )

        now = self.clock()
        for normalized_name, locations in observed.items():
            session = self.sessions.get(normalized_name)
            if session is not None and session.finalize_failures:
                # The previous network session had already passed the leave
                # grace, but its final Discord edit failed. A newly observed
                # player is a new session and must receive a fresh message.
                del self.sessions[normalized_name]
                self._save_state()
                session = None
            location = self._select_location(session, locations)
            player_name, server = location

            if session is None:
                message_id = await self.messenger.start_player_session(
                    self._channel_id,
                    player_name,
                    server.name,
                    now,
                )
                if message_id is None:
                    continue
                self.sessions[normalized_name] = PlayerSession(
                    normalized_name=normalized_name,
                    display_name=player_name,
                    server_id=server.id,
                    server_name=server.name,
                    channel_id=self._channel_id,
                    message_id=message_id,
                    started_at=now,
                )
                self._save_state()
                continue

            session_changed = False
            if session.server_id != server.id:
                message_id = await self.messenger.update_player_session(
                    session.channel_id,
                    session.message_id,
                    player_name,
                    server.name,
                    session.started_at,
                )
                if message_id is None:
                    continue
                session.message_id = message_id
                session.server_id = server.id
                session.server_name = server.name
                session_changed = True

            if session.display_name != player_name:
                session.display_name = player_name
                session_changed = True
            if session.missing_since is not None:
                session.missing_since = None
                session_changed = True
            if session_changed:
                self._save_state()

        if all_healthy:
            for normalized_name, session in list(self.sessions.items()):
                if normalized_name in observed:
                    continue
                if session.missing_since is None:
                    session.missing_since = now
                    self._save_state()
                if now - session.missing_since < self.tracking.leave_grace_seconds:
                    continue
                message_id = await self.messenger.finish_player_session(
                    session.channel_id,
                    session.message_id,
                    session.display_name,
                    session.server_name,
                    session.started_at,
                    now,
                )
                if message_id is None:
                    session.finalize_failures += 1
                    if session.finalize_failures >= MAX_FINALIZE_FAILURES:
                        LOG.error(
                            "Giving up final Discord edit for %s after %s attempts",
                            session.display_name,
                            session.finalize_failures,
                        )
                        del self.sessions[normalized_name]
                    self._save_state()
                    continue
                del self.sessions[normalized_name]
                self._save_state()
        else:
            # Only confirmed healthy snapshots count toward the leave grace.
            # Pause it across agent/Query outages to avoid false leave events
            # immediately after recovery.
            for normalized_name, session in self.sessions.items():
                if normalized_name not in observed and session.missing_since is not None:
                    session.missing_since = None
                    self._save_state()

    @property
    def _channel_id(self) -> int:
        channel_id = self.tracking.channel_id
        if channel_id is None:
            raise RuntimeError("Player tracking has no Discord channel")
        return channel_id

    @staticmethod
    def _select_location(
        session: PlayerSession | None,
        locations: list[tuple[str, RemoteServer]],
    ) -> tuple[str, RemoteServer]:
        if session is not None:
            for location in locations:
                if location[1].id == session.server_id:
                    return location
        return locations[0]

    def _load_state(self) -> None:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            raw_sessions = payload.get("sessions", [])
            if not isinstance(raw_sessions, list):
                raise ValueError("sessions must be an array")
            for raw in raw_sessions:
                session = PlayerSession(
                    normalized_name=str(raw["normalized_name"]),
                    display_name=str(raw["display_name"]),
                    server_id=str(raw["server_id"]),
                    server_name=str(raw["server_name"]),
                    channel_id=int(raw["channel_id"]),
                    message_id=int(raw["message_id"]),
                    started_at=float(raw["started_at"]),
                    missing_since=(
                        None
                        if raw.get("missing_since") is None
                        else float(raw["missing_since"])
                    ),
                    finalize_failures=int(raw.get("finalize_failures", 0)),
                )
                if (
                    not session.normalized_name
                    or not session.display_name
                    or session.channel_id <= 0
                    or session.message_id <= 0
                    or session.finalize_failures < 0
                ):
                    raise ValueError("session contains invalid values")
                self.sessions[session.normalized_name] = session
            if self.sessions:
                LOG.info("Restored %s active player session(s)", len(self.sessions))
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            LOG.exception("Could not load player session state from %s", self.state_file)
            self.sessions.clear()

    def _save_state(self) -> None:
        payload = {
            "version": 1,
            "sessions": [asdict(session) for session in self.sessions.values()],
        }
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_file)
        except OSError:
            LOG.exception("Could not save player session state to %s", self.state_file)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = ["PlayerPresenceMonitor", "PlayerSession", "PlayerSessionMessenger"]

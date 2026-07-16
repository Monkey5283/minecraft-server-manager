import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from mc_manager.client import AgentUnavailable
from mc_manager.config import (
    ControllerConfig,
    PlayerTrackingConfig,
    RemoteServer,
)
from mc_manager.player_monitor import PlayerPresenceMonitor


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_config(state_file: Path, *, leave_grace_seconds: float = 10.0):
    servers = (
        RemoteServer(
            id="lobby",
            name="Lobby",
            agent_url="http://192.168.1.35:8766",
            token="host-two",
            track_players=True,
        ),
        RemoteServer(
            id="vanilla",
            name="Vanilla",
            agent_url="http://192.168.1.16:8766",
            token="host-one",
            track_players=True,
        ),
    )
    return ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session-secret",
        cookie_secure=False,
        discord_token="discord-token",
        discord_guild_id=123,
        announcement_channel_id=456,
        servers=servers,
        player_tracking=PlayerTrackingConfig(
            enabled=True,
            channel_id=456,
            poll_interval_seconds=5,
            leave_grace_seconds=leave_grace_seconds,
            state_file=state_file,
        ),
    )


def make_agents(snapshots: dict[str, tuple[str, ...] | BaseException]):
    async def players(server: RemoteServer) -> tuple[str, ...]:
        snapshot = snapshots[server.id]
        if isinstance(snapshot, BaseException):
            raise snapshot
        return snapshot

    agents = Mock()
    agents.players = AsyncMock(side_effect=players)
    return agents


def make_messenger(*start_message_ids: int):
    messenger = Mock()
    messenger.start_player_session = AsyncMock(side_effect=start_message_ids)
    messenger.update_player_session = AsyncMock(
        side_effect=lambda _channel, message_id, *_args: message_id
    )
    messenger.finish_player_session = AsyncMock(
        side_effect=lambda _channel, message_id, *_args: message_id
    )
    return messenger


def test_runtime_server_updates_refresh_player_tracking_targets(tmp_path: Path):
    config = make_config(tmp_path / "player-sessions.json")
    monitor = PlayerPresenceMonitor(
        config,
        make_agents({"lobby": (), "vanilla": ()}),
        make_messenger(),
        state_file=tmp_path / "player-sessions.json",
    )
    creative = RemoteServer(
        id="creative",
        name="Creative",
        agent_url="http://192.168.1.35:8766",
        token="host-two",
        track_players=True,
    )

    monitor.set_servers((config.servers[0], creative))

    assert [server.id for server in monitor.servers] == ["lobby", "creative"]


async def test_join_sends_one_session_message(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ()}
    agents = make_agents(snapshots)
    messenger = make_messenger(101)
    clock = MutableClock()
    monitor = PlayerPresenceMonitor(
        make_config(state_file),
        agents,
        messenger,
        clock=clock,
        state_file=state_file,
    )

    await monitor.poll_once()

    messenger.start_player_session.assert_awaited_once_with(
        456, "Alice", "Lobby", 1_000.0
    )
    messenger.update_player_session.assert_not_awaited()
    messenger.finish_player_session.assert_not_awaited()
    assert monitor.sessions["alice"].message_id == 101


async def test_unchanged_snapshot_does_not_edit_message(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ()}
    agents = make_agents(snapshots)
    messenger = make_messenger(101)
    monitor = PlayerPresenceMonitor(
        make_config(state_file),
        agents,
        messenger,
        clock=MutableClock(),
        state_file=state_file,
    )

    await monitor.poll_once()
    await monitor.poll_once()

    messenger.start_player_session.assert_awaited_once()
    messenger.update_player_session.assert_not_awaited()
    messenger.finish_player_session.assert_not_awaited()


async def test_server_move_edits_the_same_message(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ()}
    agents = make_agents(snapshots)
    messenger = make_messenger(101)
    clock = MutableClock()
    monitor = PlayerPresenceMonitor(
        make_config(state_file),
        agents,
        messenger,
        clock=clock,
        state_file=state_file,
    )

    await monitor.poll_once()
    snapshots["lobby"] = ()
    snapshots["vanilla"] = ("Alice",)
    clock.advance(5)
    await monitor.poll_once()

    messenger.start_player_session.assert_awaited_once()
    messenger.update_player_session.assert_awaited_once_with(
        456, 101, "Alice", "Vanilla", 1_000.0
    )
    assert monitor.sessions["alice"].server_id == "vanilla"
    assert monitor.sessions["alice"].message_id == 101


async def test_transfer_overlap_keeps_old_server_until_move_is_unambiguous(
    tmp_path: Path,
):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ()}
    agents = make_agents(snapshots)
    messenger = make_messenger(101)
    monitor = PlayerPresenceMonitor(
        make_config(state_file),
        agents,
        messenger,
        clock=MutableClock(),
        state_file=state_file,
    )

    await monitor.poll_once()
    snapshots["vanilla"] = ("Alice",)
    await monitor.poll_once()

    messenger.update_player_session.assert_not_awaited()
    assert monitor.sessions["alice"].server_id == "lobby"

    snapshots["lobby"] = ()
    await monitor.poll_once()

    messenger.update_player_session.assert_awaited_once_with(
        456, 101, "Alice", "Vanilla", 1_000.0
    )
    assert monitor.sessions["alice"].server_id == "vanilla"


async def test_leave_grace_then_final_edit_and_session_removal(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ()}
    agents = make_agents(snapshots)
    messenger = make_messenger(101)
    clock = MutableClock()
    monitor = PlayerPresenceMonitor(
        make_config(state_file, leave_grace_seconds=10),
        agents,
        messenger,
        clock=clock,
        state_file=state_file,
    )

    await monitor.poll_once()
    snapshots["lobby"] = ()
    clock.advance(1)
    await monitor.poll_once()

    messenger.finish_player_session.assert_not_awaited()
    assert monitor.sessions["alice"].missing_since == 1_001.0

    clock.advance(10)
    await monitor.poll_once()

    messenger.finish_player_session.assert_awaited_once_with(
        456, 101, "Alice", "Lobby", 1_000.0, 1_011.0
    )
    assert "alice" not in monitor.sessions

    clock.advance(10)
    await monitor.poll_once()
    messenger.finish_player_session.assert_awaited_once()


async def test_rejoin_starts_a_new_session_message(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ()}
    agents = make_agents(snapshots)
    messenger = make_messenger(101, 202)
    clock = MutableClock()
    monitor = PlayerPresenceMonitor(
        make_config(state_file, leave_grace_seconds=0),
        agents,
        messenger,
        clock=clock,
        state_file=state_file,
    )

    await monitor.poll_once()
    snapshots["lobby"] = ()
    clock.advance(5)
    await monitor.poll_once()
    snapshots["lobby"] = ("Alice",)
    clock.advance(5)
    await monitor.poll_once()

    assert messenger.start_player_session.await_count == 2
    assert messenger.start_player_session.await_args_list[0].args == (
        456,
        "Alice",
        "Lobby",
        1_000.0,
    )
    assert messenger.start_player_session.await_args_list[1].args == (
        456,
        "Alice",
        "Lobby",
        1_010.0,
    )
    messenger.finish_player_session.assert_awaited_once()
    assert monitor.sessions["alice"].message_id == 202


async def test_multiple_players_keep_independent_session_messages(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ("Bob",)}
    agents = make_agents(snapshots)
    messenger = make_messenger(101, 202)
    monitor = PlayerPresenceMonitor(
        make_config(state_file),
        agents,
        messenger,
        clock=MutableClock(),
        state_file=state_file,
    )

    await monitor.poll_once()

    assert monitor.sessions["alice"].message_id == 101
    assert monitor.sessions["alice"].server_id == "lobby"
    assert monitor.sessions["bob"].message_id == 202
    assert monitor.sessions["bob"].server_id == "vanilla"

    snapshots["lobby"] = ()
    snapshots["vanilla"] = ("Bob", "Alice")
    await monitor.poll_once()

    messenger.update_player_session.assert_awaited_once_with(
        456, 101, "Alice", "Vanilla", 1_000.0
    )
    messenger.finish_player_session.assert_not_awaited()
    assert monitor.sessions["alice"].message_id == 101
    assert monitor.sessions["alice"].server_id == "vanilla"
    assert monitor.sessions["bob"].message_id == 202
    assert monitor.sessions["bob"].server_id == "vanilla"


async def test_agent_failure_does_not_create_a_false_leave(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots: dict[str, tuple[str, ...] | BaseException] = {
        "lobby": ("Alice",),
        "vanilla": (),
    }
    agents = make_agents(snapshots)
    messenger = make_messenger(101)
    clock = MutableClock()
    monitor = PlayerPresenceMonitor(
        make_config(state_file, leave_grace_seconds=5),
        agents,
        messenger,
        clock=clock,
        state_file=state_file,
    )

    await monitor.poll_once()
    snapshots["lobby"] = AgentUnavailable("Lobby agent is unreachable")
    clock.advance(60)
    await monitor.poll_once()

    messenger.finish_player_session.assert_not_awaited()
    assert monitor.sessions["alice"].missing_since is None
    assert monitor.sessions["alice"].message_id == 101


async def test_agent_failure_restarts_the_verified_leave_grace(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots: dict[str, tuple[str, ...] | BaseException] = {
        "lobby": ("Alice",),
        "vanilla": (),
    }
    messenger = make_messenger(101)
    clock = MutableClock()
    monitor = PlayerPresenceMonitor(
        make_config(state_file, leave_grace_seconds=10),
        make_agents(snapshots),
        messenger,
        clock=clock,
        state_file=state_file,
    )

    await monitor.poll_once()
    snapshots["lobby"] = ()
    clock.advance(1)
    await monitor.poll_once()
    assert monitor.sessions["alice"].missing_since == 1_001.0

    snapshots["vanilla"] = AgentUnavailable("Vanilla agent is unreachable")
    clock.advance(30)
    await monitor.poll_once()
    assert monitor.sessions["alice"].missing_since is None

    snapshots["vanilla"] = ()
    await monitor.poll_once()
    assert monitor.sessions["alice"].missing_since == 1_031.0
    messenger.finish_player_session.assert_not_awaited()

    clock.advance(10)
    await monitor.poll_once()
    messenger.finish_player_session.assert_awaited_once()


async def test_failed_final_edit_does_not_suppress_a_later_join(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ()}
    messenger = make_messenger(101, 202)
    messenger.finish_player_session.side_effect = None
    messenger.finish_player_session.return_value = None
    clock = MutableClock()
    monitor = PlayerPresenceMonitor(
        make_config(state_file, leave_grace_seconds=0),
        make_agents(snapshots),
        messenger,
        clock=clock,
        state_file=state_file,
    )

    await monitor.poll_once()
    snapshots["lobby"] = ()
    clock.advance(5)
    await monitor.poll_once()
    assert monitor.sessions["alice"].finalize_failures == 1

    snapshots["lobby"] = ("Alice",)
    clock.advance(5)
    await monitor.poll_once()

    assert messenger.start_player_session.await_count == 2
    assert monitor.sessions["alice"].message_id == 202
    assert monitor.sessions["alice"].started_at == 1_010.0


async def test_session_is_released_after_three_failed_final_edits(tmp_path: Path):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ()}
    messenger = make_messenger(101)
    messenger.finish_player_session.side_effect = None
    messenger.finish_player_session.return_value = None
    monitor = PlayerPresenceMonitor(
        make_config(state_file, leave_grace_seconds=0),
        make_agents(snapshots),
        messenger,
        clock=MutableClock(),
        state_file=state_file,
    )

    await monitor.poll_once()
    snapshots["lobby"] = ()
    await monitor.poll_once()
    await monitor.poll_once()
    await monitor.poll_once()

    assert messenger.finish_player_session.await_count == 3
    assert "alice" not in monitor.sessions


async def test_completed_discord_mutations_are_saved_if_poll_is_cancelled(
    tmp_path: Path,
):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice", "Bob"), "vanilla": ()}
    messenger = make_messenger()
    messenger.start_player_session.side_effect = [101, asyncio.CancelledError()]
    monitor = PlayerPresenceMonitor(
        make_config(state_file),
        make_agents(snapshots),
        messenger,
        clock=MutableClock(),
        state_file=state_file,
    )

    with pytest.raises(asyncio.CancelledError):
        await monitor.poll_once()

    restored = PlayerPresenceMonitor(
        make_config(state_file),
        make_agents(snapshots),
        make_messenger(),
        clock=MutableClock(),
        state_file=state_file,
    )
    assert restored.sessions["alice"].message_id == 101
    assert "bob" not in restored.sessions


async def test_persisted_session_restores_message_id_and_edits_it_after_move(
    tmp_path: Path,
):
    state_file = tmp_path / "player-sessions.json"
    snapshots = {"lobby": ("Alice",), "vanilla": ()}
    clock = MutableClock()
    first_messenger = make_messenger(777)
    first_monitor = PlayerPresenceMonitor(
        make_config(state_file),
        make_agents(snapshots),
        first_messenger,
        clock=clock,
        state_file=state_file,
    )
    await first_monitor.poll_once()

    snapshots["lobby"] = ()
    snapshots["vanilla"] = ("Alice",)
    clock.advance(5)
    restored_messenger = make_messenger()
    restored_messenger.update_player_session = AsyncMock(return_value=888)
    restored_monitor = PlayerPresenceMonitor(
        make_config(state_file),
        make_agents(snapshots),
        restored_messenger,
        clock=clock,
        state_file=state_file,
    )

    assert restored_monitor.sessions["alice"].message_id == 777
    await restored_monitor.poll_once()

    restored_messenger.start_player_session.assert_not_awaited()
    restored_messenger.update_player_session.assert_awaited_once_with(
        456, 777, "Alice", "Vanilla", 1_000.0
    )
    assert restored_monitor.sessions["alice"].server_id == "vanilla"
    assert restored_monitor.sessions["alice"].message_id == 888

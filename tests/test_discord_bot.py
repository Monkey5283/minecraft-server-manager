from pathlib import Path
from unittest.mock import AsyncMock, Mock

import discord

from mc_manager.client import AgentUnavailable
from mc_manager.config import ControllerConfig, RemoteServer, UPSConfig
from mc_manager.discord_bot import MinecraftDiscordBot
from mc_manager.health import ControllerHealthSnapshot, HealthLevel, ServerHealth
from mc_manager.ups import UPSReading


def make_health_config(*, health_presence_enabled: bool = True) -> ControllerConfig:
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
        ups=UPSConfig(
            enabled=True,
            discord_status_enabled=True,
            discord_status_channel_id=456,
        ),
        health_presence_enabled=health_presence_enabled,
    )


def make_health_snapshot(
    charge_percent: float,
    *,
    observed_at: float = 1_000.0,
    level: HealthLevel = HealthLevel.ALL_GOOD,
) -> ControllerHealthSnapshot:
    return ControllerHealthSnapshot(
        level=level,
        servers=(ServerHealth("survival", "Survival", "online"),),
        ups=UPSReading(
            available=True,
            status="OL CHRG",
            charge_percent=charge_percent,
            observed_at=observed_at,
        ),
        observed_at=observed_at,
    )


def test_successful_update_message_includes_agent_result() -> None:
    target = RemoteServer(
        id="lobby",
        name="Lobby",
        agent_url="http://192.168.1.35:8766",
        token="token",
    )

    message = MinecraftDiscordBot._job_result_message(
        target,
        {
            "state": "succeeded",
            "operation": "update",
            "output": (
                "lobby is already running Paper 26.2 BETA build 60; "
                "no restart needed"
            ),
        },
        "job-1",
    )

    assert "`update` completed" in message
    assert "already running Paper 26.2 BETA build 60" in message


def test_non_update_success_message_does_not_include_command_output() -> None:
    target = RemoteServer(
        id="lobby",
        name="Lobby",
        agent_url="http://192.168.1.35:8766",
        token="token",
    )

    message = MinecraftDiscordBot._job_result_message(
        target,
        {
            "state": "succeeded",
            "operation": "restart",
            "output": "internal command output",
        },
        "job-2",
    )

    assert "`restart` completed" in message
    assert "internal command output" not in message


async def test_online_announcement_is_sent_only_once(monkeypatch):
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session-secret",
        cookie_secure=False,
        discord_token="discord-token",
        discord_guild_id=123,
        announcement_channel_id=456,
    )
    channel = Mock()
    channel.send = AsyncMock()
    bot = MinecraftDiscordBot(config, Mock())
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)

    await bot.on_ready()
    await bot.on_ready()

    channel.send.assert_awaited_once()
    assert channel.send.await_args.args[0].endswith("\nDid you miss me?")


async def test_startup_announcement_includes_server_statuses(monkeypatch):
    servers = (
        RemoteServer(
            id="survival",
            name="Survival",
            agent_url="http://192.168.1.31:8766",
            token="one",
        ),
        RemoteServer(
            id="creative",
            name="Creative",
            agent_url="http://192.168.1.32:8766",
            token="two",
        ),
    )
    config = ControllerConfig(
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
    )
    agents = Mock()
    agents.status = AsyncMock(
        side_effect=[
            {"state": "online"},
            AgentUnavailable("Creative agent is unreachable"),
        ]
    )
    channel = Mock()
    channel.send = AsyncMock()
    bot = MinecraftDiscordBot(config, agents)
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)

    await bot.on_ready()
    await bot.on_ready()

    assert channel.send.await_count == 2
    status_message = channel.send.await_args_list[1].args[0]
    assert "**Survival** — online" in status_message
    assert "**Creative** — unreachable" in status_message
    assert agents.status.await_count == 2


async def test_player_session_uses_one_editable_message(monkeypatch):
    config = ControllerConfig(
        bind="127.0.0.1",
        port=8080,
        web_username="admin",
        web_password="password",
        session_secret="session-secret",
        cookie_secure=False,
        discord_token="discord-token",
        discord_guild_id=123,
        announcement_channel_id=456,
    )
    sent_message = Mock()
    sent_message.id = 789
    partial_message = Mock()
    partial_message.edit = AsyncMock()
    channel = Mock()
    channel.send = AsyncMock(return_value=sent_message)
    channel.get_partial_message = Mock(return_value=partial_message)
    bot = MinecraftDiscordBot(config, Mock())
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)

    message_id = await bot.start_player_session(456, "Monkey_5283", "Lobby", 1000)
    moved_id = await bot.update_player_session(
        456, message_id, "Monkey_5283", "Vanilla", 1000
    )
    finished_id = await bot.finish_player_session(
        456, message_id, "Monkey_5283", "Vanilla", 1000, 1100
    )

    assert message_id == moved_id == finished_id == 789
    channel.send.assert_awaited_once()
    assert "joined Minecraft" in channel.send.await_args.args[0]
    assert "Lobby" in channel.send.await_args.args[0]
    assert partial_message.edit.await_count == 2
    assert "Current server: **Vanilla**" in partial_message.edit.await_args_list[0].kwargs[
        "content"
    ]
    assert "left Minecraft" in partial_message.edit.await_args_list[1].kwargs["content"]


async def test_startup_does_not_send_a_separate_static_ups_message(monkeypatch):
    config = make_health_config()
    channel = Mock()
    channel.send = AsyncMock()
    bot = MinecraftDiscordBot(config, Mock())
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)

    await bot.on_ready()

    channel.send.assert_awaited_once()
    assert "Minecraft Manager is online" in channel.send.await_args.args[0]


async def test_first_health_publish_sends_ups_card_and_presence(monkeypatch):
    sent_message = Mock()
    sent_message.id = 900
    channel = Mock()
    channel.send = AsyncMock(return_value=sent_message)
    bot = MinecraftDiscordBot(make_health_config(), Mock())
    bot.change_presence = AsyncMock()
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)

    await bot.publish_health(make_health_snapshot(100))

    channel.send.assert_awaited_once()
    card = channel.send.await_args.args[0]
    assert "Battery Backup Online and Ready" in card
    assert "Battery: **100%**" in card
    assert "Monitoring:" in card
    bot.change_presence.assert_awaited_once()
    presence = bot.change_presence.await_args.kwargs
    assert presence["status"] is discord.Status.online
    assert presence["activity"].type is discord.ActivityType.watching
    assert presence["activity"].name.endswith("All Good")


async def test_health_presence_is_reapplied_after_discord_reconnect(monkeypatch):
    sent_message = Mock()
    sent_message.id = 900
    channel = Mock()
    channel.send = AsyncMock(return_value=sent_message)
    bot = MinecraftDiscordBot(make_health_config(), Mock())
    bot.change_presence = AsyncMock()
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)

    await bot.publish_health(make_health_snapshot(100))
    bot._online_announced = True
    await bot.on_ready()

    assert bot.change_presence.await_count == 2
    reconnect_presence = bot.change_presence.await_args.kwargs
    assert reconnect_presence["status"] is discord.Status.online
    assert reconnect_presence["activity"].name.endswith("All Good")


async def test_unknown_ups_status_is_not_rendered_as_online_and_ready(monkeypatch):
    sent_message = Mock()
    sent_message.id = 900
    channel = Mock()
    channel.send = AsyncMock(return_value=sent_message)
    bot = MinecraftDiscordBot(
        make_health_config(health_presence_enabled=False),
        Mock(),
    )
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)
    snapshot = ControllerHealthSnapshot(
        level=HealthLevel.ATTENTION,
        servers=(ServerHealth("survival", "Survival", "failed"),),
        ups=UPSReading(True, "unknown", None, 1_000),
        observed_at=1_000,
    )

    await bot.publish_health(snapshot)

    card = channel.send.await_args.args[0]
    assert "Battery Backup Online and Ready" not in card
    assert "Power: **Unknown**" in card
    assert "1 failed" in card
    assert "Attention" in card


async def test_changed_charge_edits_same_card_and_identical_snapshot_is_ignored(
    monkeypatch,
):
    sent_message = Mock()
    sent_message.id = 900
    partial_message = Mock()
    partial_message.edit = AsyncMock()
    channel = Mock()
    channel.send = AsyncMock(return_value=sent_message)
    channel.get_partial_message = Mock(return_value=partial_message)
    bot = MinecraftDiscordBot(make_health_config(), Mock())
    bot.change_presence = AsyncMock()
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)

    await bot.publish_health(make_health_snapshot(100, observed_at=1_000))
    await bot.publish_health(make_health_snapshot(95, observed_at=1_015))

    channel.send.assert_awaited_once()
    channel.get_partial_message.assert_called_once_with(900)
    partial_message.edit.assert_awaited_once()
    assert "Battery: **95%**" in partial_message.edit.await_args.kwargs["content"]
    bot.change_presence.assert_awaited_once()

    await bot.publish_health(make_health_snapshot(95, observed_at=1_030))

    channel.send.assert_awaited_once()
    partial_message.edit.assert_awaited_once()
    bot.change_presence.assert_awaited_once()


async def test_persisted_ups_card_is_edited_after_restart(
    tmp_path: Path,
    monkeypatch,
):
    state_file = tmp_path / "ups-status-card.json"
    config = make_health_config(health_presence_enabled=False)
    sent_message = Mock()
    sent_message.id = 900
    first_channel = Mock()
    first_channel.send = AsyncMock(return_value=sent_message)
    first_bot = MinecraftDiscordBot(
        config,
        Mock(),
        health_state_file=state_file,
    )
    monkeypatch.setattr(first_bot, "is_ready", lambda: True)
    monkeypatch.setattr(first_bot, "get_channel", lambda _channel_id: first_channel)

    await first_bot.publish_health(make_health_snapshot(100))

    assert state_file.exists()

    restored_message = Mock()
    restored_message.edit = AsyncMock()
    restored_channel = Mock()
    restored_channel.send = AsyncMock()
    restored_channel.get_partial_message = Mock(return_value=restored_message)
    restored_bot = MinecraftDiscordBot(
        config,
        Mock(),
        health_state_file=state_file,
    )
    monkeypatch.setattr(restored_bot, "is_ready", lambda: True)
    monkeypatch.setattr(
        restored_bot,
        "get_channel",
        lambda _channel_id: restored_channel,
    )

    await restored_bot.publish_health(make_health_snapshot(95, observed_at=1_015))

    restored_channel.send.assert_not_awaited()
    restored_channel.get_partial_message.assert_called_once_with(900)
    restored_message.edit.assert_awaited_once()
    assert "Battery: **95%**" in restored_message.edit.await_args.kwargs["content"]


async def test_failed_card_state_save_is_retried_before_changed_edit(monkeypatch):
    sent_message = Mock()
    sent_message.id = 900
    partial_message = Mock()
    partial_message.edit = AsyncMock()
    channel = Mock()
    channel.send = AsyncMock(return_value=sent_message)
    channel.get_partial_message = Mock(return_value=partial_message)
    bot = MinecraftDiscordBot(
        make_health_config(health_presence_enabled=False),
        Mock(),
    )
    save_state = Mock(side_effect=[False, True])
    bot._save_ups_card_state = save_state
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)

    await bot.publish_health(make_health_snapshot(100))
    await bot.publish_health(make_health_snapshot(95, observed_at=1_015))

    assert save_state.call_count == 2
    assert bot._ups_card_state_dirty is False
    partial_message.edit.assert_awaited_once()


async def test_temporary_forbidden_edit_keeps_original_card_id(monkeypatch):
    response = Mock()
    response.status = 403
    response.reason = "Forbidden"
    forbidden = discord.Forbidden(response, "missing permission")
    partial_message = Mock()
    partial_message.edit = AsyncMock(side_effect=forbidden)
    channel = Mock()
    channel.get_partial_message = Mock(return_value=partial_message)
    channel.send = AsyncMock(side_effect=forbidden)
    bot = MinecraftDiscordBot(
        make_health_config(health_presence_enabled=False),
        Mock(),
    )
    bot._ups_card_channel_id = 456
    bot._ups_card_message_id = 900
    bot._ups_card_signature = bot._ups_status_signature(make_health_snapshot(100))
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)
    changed = make_health_snapshot(95, observed_at=1_015)

    await bot.publish_health(changed)

    assert bot._ups_card_message_id == 900
    partial_message.edit.side_effect = None
    await bot.publish_health(changed)

    assert bot._ups_card_message_id == 900
    assert partial_message.edit.await_count == 2
    assert channel.send.await_count == 1

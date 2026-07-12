from unittest.mock import AsyncMock, Mock

from mc_manager.client import AgentUnavailable
from mc_manager.config import ControllerConfig, RemoteServer, UPSConfig
from mc_manager.discord_bot import MinecraftDiscordBot


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


async def test_startup_announcement_includes_ups_ready_when_enabled(monkeypatch):
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
        ups=UPSConfig(enabled=True),
    )
    channel = Mock()
    channel.send = AsyncMock()
    bot = MinecraftDiscordBot(config, Mock())
    monkeypatch.setattr(bot, "get_channel", lambda _channel_id: channel)

    await bot.on_ready()

    assert channel.send.await_count == 2
    assert channel.send.await_args_list[1].args[0] == "🔋 Battery Backup Online and Ready"

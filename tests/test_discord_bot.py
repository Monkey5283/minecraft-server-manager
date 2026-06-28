from unittest.mock import AsyncMock, Mock

from mc_manager.client import AgentUnavailable
from mc_manager.config import ControllerConfig, RemoteServer
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

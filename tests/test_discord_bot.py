from unittest.mock import AsyncMock, Mock

from mc_manager.config import ControllerConfig
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

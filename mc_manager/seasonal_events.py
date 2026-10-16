"""Announce the lobby plugin's actual event through the existing authenticated agent."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from .client import AgentUnavailable

LOG = logging.getLogger("mc_manager.seasons")
STATUS_PATH = "plugins/MonkeySeasons/event-status.json"


def current_event(content: str, now: float) -> dict | None:
    if len(content) > 16384:
        return None
    try:
        event = json.loads(content)
        if not isinstance(event, dict) or event.get("schema") != 1 or event.get("active") is not True:
            return None
        updated = event.get("updated_at")
        if not isinstance(updated, (int, float)) or not now - 300 <= updated <= now + 60:
            return None
        if any(not isinstance(event.get(key), str) or not 1 <= len(event[key]) <= 200
               for key in ("event_key", "theme", "title")):
            return None
        end = event.get("end_exclusive")
        start = event.get("start")
        if event.get("mode") == "auto":
            if not isinstance(start, int) or not isinstance(end, int) or not start <= now < end:
                return None
        return event
    except (ValueError, TypeError):
        return None


def event_message(event: dict) -> str:
    # Plugin-owned titles are displayed as text; Discord mentions are disabled at send.
    title = event["title"].replace("`", "").replace("*", "").replace("\n", " ")
    lines = [f"🎉 **Monkeycraft seasonal event: {title} is on!**",
             "Visit the **Lobby** for themed parkour, arcade and arena minigames, seasonal quests, "
             "exploration shops, cosmetics, decorations and holiday music.",
             "**Right-click the event board at spawn or walk into the parkour arch** to get started."]
    if event.get("mode") == "auto":
        lines.append(f"Runs <t:{event['start']}:D> through <t:{event['end_exclusive'] - 1}:D> "
                     f"(event calendar: {event.get('timezone', 'America/Edmonton')}).")
    else:
        lines.append("This event is currently enabled by the server team.")
    if event["theme"] == "christmas":
        lines.append("🎄 Visit the giant Christmas tree! Christmas Eve, Christmas Day and "
                     "New Year's Eve/Day are part of the celebration.")
    return "\n".join(lines)


class SeasonalEventMonitor:
    def __init__(self, config, agents, bot, state_file: Path):
        self.config, self.agents, self.bot, self.state_file = config, agents, bot, state_file
        self.seen: list[str] = []
        try:
            value = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(value, list):
                self.seen = [key for key in value if isinstance(key, str)][-64:]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            LOG.warning("Could not load seasonal announcement history", exc_info=True)

    async def poll(self) -> None:
        target = self.bot.servers.get(self.config.seasonal_server_id)
        channel = self.config.seasonal_channel_id or self.config.announcement_channel_id
        if target is None or not channel or not self.bot.is_ready():
            return
        try:
            status = await self.agents.status(target)
            if status.get("state") != "online":
                return
            result = await self.agents.file_content(target, STATUS_PATH)
            event = current_event(result["content"], time.time())
        except AgentUnavailable as exc:
            if exc.status_code not in (403, 404, 409):
                LOG.warning("Seasonal status unavailable: %s", exc)
            return
        except (KeyError, TypeError):
            LOG.warning("Agent returned an invalid seasonal status response")
            return
        if event is None:
            return
        key = f"{channel}:{target.id}:{event['event_key']}"
        if key in self.seen:
            return
        if not await self.bot.announce_seasonal(channel, event_message(event)):
            return
        self.seen = [*self.seen, key][-64:]
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.seen), encoding="utf-8")
            temporary.replace(self.state_file)
        except OSError:
            LOG.error("Could not persist seasonal announcement history", exc_info=True)

    async def run(self) -> None:
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Seasonal event check failed; retrying next minute")
            await asyncio.sleep(60)

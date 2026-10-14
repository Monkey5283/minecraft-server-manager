# Seasonal event announcements

The controller checks the Lobby's `plugins/MonkeySeasons/event-status.json` once
per minute through the existing authenticated agent file API. No Discord token,
webhook, extra network port or agent update is required in the lobby plugin.

Under `[discord]` in controller.toml, `seasonal_announcements_enabled` defaults
to true, `seasonal_server_id` defaults to `lobby`, and `seasonal_channel_id`
defaults to the existing `announcement_channel_id`. Set the enable flag false
to disable automatic posts. Posts suppress Discord mentions.

The bot sends one announcement for an active event, with GUI instructions and
the event's date range. Successfully announced event keys persist in
`seasonal-announcements.json` beside the paired-agent registry. Reconnecting or
restarting does not repost the same event. Failed sends retry next minute.
An active event missed during controller downtime is announced on recovery.

The plugin status is schema 1 JSON, with boolean `active`, Unix-second
`updated_at`, `event_key`, `theme`, `title`, `timezone`, and `mode`. Automatic
events supply Unix-second `start` and `end_exclusive`; pinned themes supply no
promised end date. The plugin refreshes the file every minute and atomically
replaces it. The bot ignores offline servers, missing plugins, inactive events,
malformed status and heartbeats older than five minutes.

MonkeySeasons 4.1 keeps Christmas active from December 1 through January 1;
the New Year theme starts January 2 in America/Edmonton. The plugin setting
`christmas-through-new-years-day: false` restores the January 1 handoff.
Lobby visitors receive a clickable welcome, event-change announcement and
30-minute reminder. `announcements.reminder-minutes: 0` disables reminders;
`announcements.enabled: false` disables change/reminder broadcasts, and
`join-message: false` disables welcomes. `announcements.discord-status: false`
publishes inactive status to disable Discord event posts.

Validate with `pytest tests/test_seasonal_events.py tests/test_discord_bot.py
tests/test_config.py tests/test_controller_files.py`. Date and export boundary
tests live in the private MonkeySeasons plugin project.

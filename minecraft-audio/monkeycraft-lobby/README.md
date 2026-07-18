# The Nexus Awaits

Two original, vocal-free fantasy lobby tracks for Monkeycraft. Both run exactly
15:00 and return cleanly to their opening harmony so they can loop.

## Files

- `monkeycraft_nexus_awaits.nbs` — compact Note Block Studio file for server-side
  playback through a compatible Paper/Spigot plugin.
- `monkeycraft_nexus_awaits.mid` — editable composition source.
- `monkeycraft_nexus_awaits.wav` — stereo 22.05 kHz listening master.
- `monkeycraft_festival_of_the_skyways.nbs` — upbeat plugin-ready companion.
- `monkeycraft_festival_of_the_skyways.mid` — editable upbeat composition source.
- `monkeycraft_festival_of_the_skyways.wav` — upbeat stereo listening master.
- `generate_lobby_track.py` — deterministic source generator.

The arrangement has fifteen one-minute chapters. Harp, warm strings, flute,
lute, bells, and restrained ceremonial percussion rise and fall so the lobby
does not feel like a short phrase repeated for fifteen minutes.

`Festival of the Skyways` is the brighter 120 BPM counterpart, with sparkling
harp, dancing lute offbeats, walking bass, soaring flute, bells, and a light
festival pulse. Its fifteen chapters gradually build toward a celebratory close.

## Using it on the lobby server

The `.nbs` file is intended for a Note Block Studio-compatible server plugin.
Copy it into that plugin's song directory, configure its lobby/world playback
to repeat, and keep the plugin volume modest. Exact commands and directories
vary by plugin; confirm them against the plugin you install.

The NBS metadata also requests an infinite loop from tick zero. Plugins that
ignore NBS loop metadata should be configured to replay it after 900 seconds.

The WAV is a listening master. Vanilla clients cannot receive arbitrary WAV
audio from a server plugin alone. For full-quality playback, encode the WAV as
OGG and distribute it in a server resource pack, or use a web-audio/voice-chat
solution supported by every intended client.

## Regenerating

Run with a Python environment that includes NumPy:

```powershell
python generate_lobby_track.py
python generate_lobby_track.py --variant upbeat
```

Use `--skip-wav` when only the NBS and MIDI assets are needed.

## Rights

This composition and its generated assets were created specifically for this
project. The project owner may use, modify, publish, and redistribute them,
including on the Monkeycraft servers and in its resource packs and plugins.

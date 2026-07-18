#!/usr/bin/env python3
"""Generate the original 15-minute Monkeycraft lobby theme.

Outputs:
  - monkeycraft_nexus_awaits.nbs  (Note Block Studio / NoteBlockAPI)
  - monkeycraft_nexus_awaits.mid  (editable MIDI source)
  - monkeycraft_nexus_awaits.wav  (stereo listening master)

The generator is deterministic and uses only Python plus NumPy for WAV rendering.
"""

from __future__ import annotations

import argparse
import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as np
except ImportError:  # The NBS and MIDI outputs do not require NumPy.
    np = None


TITLE = "The Nexus Awaits"
AUTHOR = "Monkeycraft"
DESCRIPTION = "Original 15-minute instrumental fantasy lobby theme; seamless loop."
BPM = 100
TICKS_PER_BEAT = 6
TICKS_PER_SECOND = 10
BEATS_PER_BAR = 4
TICKS_PER_BAR = TICKS_PER_BEAT * BEATS_PER_BAR
BARS = 375
DURATION_SECONDS = 15 * 60

# General MIDI pitches. D natural minor, with occasional B-natural for a Dorian glow.
CHORDS = {
    "Dm": (50, 57, 62, 65),
    "Bb": (46, 53, 58, 62),
    "F": (41, 53, 57, 60),
    "C": (48, 55, 60, 64),
    "Gm": (43, 50, 55, 58),
    "A": (45, 52, 57, 61),
    "Am": (45, 52, 57, 60),
}
PROGRESSION = ("Dm", "Bb", "F", "C", "Gm", "Dm", "C", "A")

CHAPTERS = (
    "Dawn at the Nexus",
    "The Lantern Road",
    "Courtyard of Banners",
    "The Moonwell",
    "Across the Skybridge",
    "The Forgotten Archive",
    "Hearthlight",
    "The Astral Gate",
    "Rain in the High Garden",
    "Crownspire",
    "Where the Wyverns Fly",
    "Homeward Path",
    "The Many Worlds",
    "Starlit Hush",
    "The Nexus Awaits",
)

UPBEAT_CHORDS = {
    "G": (43, 50, 55, 59),
    "D": (38, 50, 54, 57),
    "Em": (40, 47, 52, 55),
    "C": (36, 48, 52, 55),
    "Am": (45, 52, 57, 60),
    "Bm": (47, 54, 59, 62),
}
UPBEAT_PROGRESSION = ("G", "D", "Em", "C", "G", "Am", "C", "D")


@dataclass(frozen=True)
class Note:
    start: float
    duration: float
    pitch: int
    instrument: str
    velocity: float
    pan: float = 0.0


NBS_INSTRUMENT = {
    "harp": 0,
    "bass": 1,
    "drum": 2,
    "snare": 3,
    "hat": 4,
    "lute": 5,
    "flute": 6,
    "bell": 7,
    "chime": 8,
    "strings": 0,
}

MIDI_PROGRAM = {
    "harp": 46,
    "bass": 32,
    "lute": 24,
    "flute": 73,
    "bell": 10,
    "chime": 14,
    "strings": 48,
}


def beat_time(bar: int, beat: float = 0.0) -> float:
    return (bar * BEATS_PER_BAR + beat) * 60.0 / BPM


def add_note(notes: list[Note], bar: int, beat: float, beats: float, pitch: int,
             instrument: str, velocity: float, pan: float = 0.0) -> None:
    notes.append(Note(beat_time(bar, beat), beats * 60.0 / BPM, pitch,
                      instrument, velocity, pan))


def melody_for_bar(bar_in_phrase: int, root_name: str, variation: int) -> list[tuple[float, float, int]]:
    """A singable, original eight-bar theme with small deterministic variations."""
    root = CHORDS[root_name][0]
    scale = (0, 2, 3, 5, 7, 8, 10, 12, 14, 15, 17)
    contours = (
        (4, 5, 7, 5, 4),
        (2, 4, 5, 7, 5),
        (7, 8, 7, 5, 4),
        (5, 4, 2, 4, 5),
        (4, 7, 8, 7, 5),
        (2, 4, 5, 4, 2),
        (7, 5, 4, 2, 4),
        (5, 4, 2, 1, 0),
    )
    degrees = contours[bar_in_phrase % 8]
    if variation % 3 == 1:
        degrees = tuple(min(10, d + (1 if i in (1, 3) else 0)) for i, d in enumerate(degrees))
    elif variation % 3 == 2:
        degrees = tuple(reversed(degrees))
    rhythm = ((0.0, 1.0), (1.0, 0.5), (1.5, 1.0), (2.5, 0.5), (3.0, 1.0))
    return [(beat, duration, root + 12 + scale[degree])
            for (beat, duration), degree in zip(rhythm, degrees)]


def compose() -> list[Note]:
    notes: list[Note] = []

    for bar in range(BARS):
        chapter = bar // 25
        within = bar % 25
        transition = within == 24
        chord_name = ("A" if chapter < 14 else "Dm") if transition else PROGRESSION[within % 8]
        chord = CHORDS[chord_name]
        energy = (0.72, 0.82, 0.92, 0.70, 0.96, 0.70, 0.86, 0.92,
                  0.68, 1.00, 0.92, 0.78, 1.00, 0.58, 0.90)[chapter]

        # Warm sustained harmony: open fifth below, then a gently moving upper triad.
        add_note(notes, bar, 0, 4, chord[0] - 12, "strings", 0.19 * energy, -0.25)
        for i, pitch in enumerate(chord[1:]):
            add_note(notes, bar, 0, 4, pitch, "strings", 0.13 * energy, (-0.5, 0.15, 0.5)[i])

        # Bass breathes in half-time, leaving room for a lobby rather than demanding attention.
        if chapter not in (3, 8, 13) or bar % 2 == 0:
            add_note(notes, bar, 0, 1.8, chord[0] - 12, "bass", 0.28 * energy, -0.1)
            add_note(notes, bar, 2, 1.5, chord[1] - 12, "bass", 0.18 * energy, 0.1)

        # Harp ostinato. The last chapter echoes chapter one so the loop feels intentional.
        arp = (chord[0], chord[1], chord[2], chord[3], chord[2], chord[1], chord[2], chord[1])
        if chapter in (4, 9, 10, 12):
            arp = tuple(p + (12 if i in (3, 6) else 0) for i, p in enumerate(arp))
        for i, pitch in enumerate(arp):
            add_note(notes, bar, i * 0.5, 0.42, pitch + 12, "harp",
                     (0.22 if i % 2 else 0.30) * energy, -0.55 + i * 0.16)

        # A lute countermelody appears in earthier chapters.
        if chapter in (2, 5, 6, 9, 11, 12, 14) and not transition:
            lute_line = (chord[0] + 12, chord[2] + 12, chord[1] + 12, chord[3] + 12)
            for i, pitch in enumerate(lute_line):
                add_note(notes, bar, i, 0.72, pitch, "lute", 0.22 * energy, 0.32)

        # Main flute theme: introduced gradually, rested often, grander near the close.
        melody_active = chapter not in (0, 5, 7, 13) and within not in (6, 7, 14, 15, 22, 23, 24)
        if chapter == 14:
            melody_active = within < 16 or within in (20, 21)
        if melody_active:
            for beat, duration, pitch in melody_for_bar(within % 8, chord_name, chapter):
                add_note(notes, bar, beat, duration * 0.92, pitch, "flute", 0.25 * energy, 0.18)

        # Bell and chime landmarks keep the long piece legible without constant percussion.
        if within in (0, 8, 16):
            add_note(notes, bar, 0, 2.5, chord[2] + 24, "bell", 0.22 * energy, 0.55)
        if chapter in (3, 7, 8, 13) and bar % 2 == 0:
            add_note(notes, bar, 2, 1.8, chord[1] + 24, "chime", 0.14 * energy, -0.5)

        # Very light ceremonial pulse in the more active chapters.
        if chapter in (4, 6, 9, 10, 12):
            add_note(notes, bar, 0, 0.15, 36, "drum", 0.13 * energy)
            add_note(notes, bar, 2, 0.15, 36, "drum", 0.09 * energy)
            if bar % 2:
                add_note(notes, bar, 3, 0.12, 42, "hat", 0.07 * energy, 0.25)

    return sorted(notes, key=lambda n: (n.start, n.instrument, n.pitch))


def upbeat_melody(bar_in_phrase: int, chord_name: str, variation: int) -> list[tuple[float, float, int]]:
    """A buoyant eight-bar festival theme in G major."""
    root = UPBEAT_CHORDS[chord_name][0]
    scale = (0, 2, 4, 5, 7, 9, 11, 12, 14, 16, 17, 19)
    contours = (
        (4, 5, 7, 9, 7, 5),
        (7, 9, 10, 9, 7, 5),
        (2, 4, 5, 7, 9, 7),
        (9, 7, 5, 4, 2, 4),
        (4, 7, 9, 11, 9, 7),
        (5, 7, 9, 7, 5, 4),
        (9, 10, 9, 7, 5, 7),
        (7, 5, 4, 2, 1, 0),
    )
    degrees = contours[bar_in_phrase % 8]
    if variation % 4 == 1:
        degrees = tuple(min(11, d + (1 if i in (1, 4) else 0)) for i, d in enumerate(degrees))
    elif variation % 4 == 2:
        degrees = (degrees[0], degrees[2], degrees[1], degrees[3], degrees[5], degrees[4])
    elif variation % 4 == 3:
        degrees = tuple(max(0, d - (1 if i in (0, 3) else 0)) for i, d in enumerate(degrees))
    rhythm = ((0.0, 0.5), (0.5, 0.5), (1.0, 1.0), (2.0, 0.5), (2.5, 0.5), (3.0, 1.0))
    return [(beat, duration, root + 12 + scale[degree])
            for (beat, duration), degree in zip(rhythm, degrees)]


def compose_upbeat() -> list[Note]:
    """Compose a brighter 15-minute fantasy festival companion track."""
    notes: list[Note] = []
    energy_curve = (0.78, 0.90, 0.98, 0.84, 1.00, 0.88, 0.96, 0.82,
                    0.94, 1.00, 0.90, 0.98, 1.00, 0.76, 1.00)

    for bar in range(BARS):
        chapter = bar // 30
        within = bar % 30
        transition = within >= 28
        chord_name = ("D" if within == 28 else "G") if transition else UPBEAT_PROGRESSION[within % 8]
        chord = UPBEAT_CHORDS[chord_name]
        energy = energy_curve[chapter]

        # Broad open harmony, voiced lightly so the fast parts stay clear.
        add_note(notes, bar, 0, 4, chord[0] - 12, "strings", 0.14 * energy, -0.35)
        for i, pitch in enumerate(chord[1:]):
            add_note(notes, bar, 0, 4, pitch, "strings", 0.095 * energy, (-0.45, 0.1, 0.45)[i])

        # Walking festival bass alternates root and fifth on every beat.
        bass_line = (chord[0] - 12, chord[1] - 12, chord[0] - 12, chord[1] - 12)
        for beat, pitch in enumerate(bass_line):
            add_note(notes, bar, beat, 0.72, pitch, "bass", (0.23 if beat in (0, 2) else 0.17) * energy,
                     -0.12 if beat % 2 == 0 else 0.12)

        # Sparkling harp eighth notes, widened across the stereo field.
        arp = (chord[0], chord[2], chord[1], chord[3], chord[2], chord[3], chord[1], chord[2])
        if chapter in (4, 9, 12, 14):
            arp = tuple(p + (12 if i in (3, 5, 7) else 0) for i, p in enumerate(arp))
        for i, pitch in enumerate(arp):
            add_note(notes, bar, i * 0.5, 0.38, pitch + 12, "harp",
                     (0.25 if i % 2 == 0 else 0.19) * energy, -0.65 + i * 0.18)

        # Lute provides the dance-like offbeat lift.
        if chapter not in (7, 13) or within < 16:
            for beat, pitch in ((0.5, chord[1] + 12), (1.5, chord[2] + 12),
                                (2.5, chord[1] + 12), (3.5, chord[3] + 12)):
                add_note(notes, bar, beat, 0.34, pitch, "lute", 0.25 * energy, 0.38)

        # The flute trades full phrases with two-bar rests so the melody can breathe.
        melody_active = within < 24 and within % 8 not in (6, 7)
        if chapter == 13:
            melody_active = within in range(8, 14) or within in range(16, 22)
        if melody_active:
            for beat, duration, pitch in upbeat_melody(within % 8, chord_name, chapter):
                add_note(notes, bar, beat, duration * 0.86, pitch, "flute", 0.27 * energy, 0.2)

        # Bells mark phrases; an extra answering chime appears in the magical chapters.
        if within in (0, 8, 16, 24):
            add_note(notes, bar, 0, 1.8, chord[2] + 24, "bell", 0.24 * energy, 0.58)
        if chapter in (3, 7, 10, 13) and bar % 4 == 2:
            add_note(notes, bar, 2.5, 1.2, chord[1] + 24, "chime", 0.16 * energy, -0.58)

        # Soft four-on-the-floor pulse with snare and hats: upbeat, never nightclub-heavy.
        for beat in range(4):
            add_note(notes, bar, beat, 0.12, 36, "drum", (0.12 if beat in (0, 2) else 0.08) * energy)
        add_note(notes, bar, 1, 0.10, 38, "snare", 0.10 * energy, -0.15)
        add_note(notes, bar, 3, 0.10, 38, "snare", 0.11 * energy, 0.15)
        for beat in (0.5, 1.5, 2.5, 3.5):
            add_note(notes, bar, beat, 0.08, 42, "hat", 0.055 * energy, 0.3)

    return sorted(notes, key=lambda n: (n.start, n.instrument, n.pitch))


def configure_variant(variant: str) -> tuple[str, callable]:
    global TITLE, DESCRIPTION, BPM, TICKS_PER_BEAT, TICKS_PER_SECOND, TICKS_PER_BAR, BARS
    if variant == "upbeat":
        TITLE = "Festival of the Skyways"
        DESCRIPTION = "Original upbeat 15-minute instrumental fantasy lobby theme; seamless loop."
        BPM = 120
        TICKS_PER_BEAT = 5
        TICKS_PER_SECOND = 10
        TICKS_PER_BAR = TICKS_PER_BEAT * BEATS_PER_BAR
        BARS = 450
        return "monkeycraft_festival_of_the_skyways", compose_upbeat
    return "monkeycraft_nexus_awaits", compose


def write_string(handle, value: str) -> None:
    data = value.encode("utf-8")
    handle.write(struct.pack("<I", len(data)))
    handle.write(data)


def write_nbs(path: Path, notes: list[Note]) -> None:
    layers = ("Strings - low", "Strings - middle I", "Strings - middle II",
              "Strings - high", "Deep drone", "Harp", "Lute", "Flute", "Bells", "Pulse")
    layer_for = {"bass": 4, "harp": 5, "lute": 6, "flute": 7,
                 "bell": 8, "chime": 8, "drum": 9, "snare": 9, "hat": 9}
    events: dict[int, list[tuple[int, Note]]] = {}
    string_count_at_tick: dict[int, int] = {}
    for note in notes:
        tick = min(TICKS_PER_SECOND * DURATION_SECONDS - 1, round(note.start * TICKS_PER_SECOND))
        if note.instrument == "strings":
            layer = min(3, string_count_at_tick.get(tick, 0))
            string_count_at_tick[tick] = layer + 1
        else:
            layer = layer_for[note.instrument]
        events.setdefault(tick, []).append((layer, note))

    with path.open("wb") as handle:
        handle.write(struct.pack("<HBBHH", 0, 5, 16, TICKS_PER_SECOND * DURATION_SECONDS, len(layers)))
        write_string(handle, TITLE)
        write_string(handle, AUTHOR)
        write_string(handle, AUTHOR)
        write_string(handle, DESCRIPTION)
        handle.write(struct.pack("<HBBBIIIII", TICKS_PER_SECOND * 100, 0, 10, 4, 0, 0, 0, len(notes), 0))
        write_string(handle, "monkeycraft_nexus_awaits.mid")
        handle.write(struct.pack("<BBH", 1, 0, 0))  # Loop forever from the beginning.

        previous_tick = -1
        for tick in sorted(events):
            handle.write(struct.pack("<H", tick - previous_tick))
            previous_tick = tick
            previous_layer = -1
            # NBS permits one note per layer per tick. Keep the strongest on collisions.
            layer_notes: dict[int, Note] = {}
            for layer, note in events[tick]:
                if layer not in layer_notes or note.velocity > layer_notes[layer].velocity:
                    layer_notes[layer] = note
            for layer, note in sorted(layer_notes.items()):
                handle.write(struct.pack("<H", layer - previous_layer))
                previous_layer = layer
                # NBS key 0 is MIDI A0 (21); key 45 is Minecraft's native F#4.
                # Fold into vanilla note blocks' two-octave range so server-side
                # playback works without forcing a resource pack on lobby users.
                nbs_key = max(0, min(87, note.pitch - 21))
                while nbs_key < 33:
                    nbs_key += 12
                while nbs_key > 57:
                    nbs_key -= 12
                velocity = max(1, min(100, round(note.velocity * 180)))
                panning = max(0, min(200, round(100 + note.pan * 80)))
                handle.write(struct.pack("<BBBBh", NBS_INSTRUMENT[note.instrument], nbs_key,
                                         velocity, panning, 0))
            handle.write(struct.pack("<H", 0))
        handle.write(struct.pack("<H", 0))

        for layer in layers:
            write_string(handle, layer)
            handle.write(struct.pack("<BBB", 0, 100, 100))
        handle.write(struct.pack("<B", 0))  # No custom instruments.


def vlq(value: int) -> bytes:
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(out)


def write_midi(path: Path, notes: list[Note]) -> None:
    ppq = 480
    tempo = round(60_000_000 / BPM)
    instruments = ("strings", "bass", "harp", "lute", "flute", "bell", "chime")
    channels = {name: i for i, name in enumerate(instruments)}
    events: list[tuple[int, int, bytes]] = []
    events.append((0, 0, b"\xFF\x51\x03" + tempo.to_bytes(3, "big")))
    events.append((0, 0, b"\xFF\x58\x04\x04\x02\x18\x08"))
    title = TITLE.encode("utf-8")
    events.append((0, 0, b"\xFF\x03" + vlq(len(title)) + title))
    for name, channel in channels.items():
        events.append((0, 0, bytes([0xC0 | channel, MIDI_PROGRAM[name]])))
    for note in notes:
        if note.instrument in ("drum", "snare", "hat"):
            channel = 9
            pitch = {"drum": 36, "snare": 38, "hat": 42}[note.instrument]
        else:
            channel = channels[note.instrument]
            pitch = note.pitch
        start = round(note.start * BPM / 60 * ppq)
        end = round((note.start + note.duration) * BPM / 60 * ppq)
        velocity = max(1, min(127, round(note.velocity * 210)))
        events.append((start, 2, bytes([0x90 | channel, pitch, velocity])))
        events.append((end, 1, bytes([0x80 | channel, pitch, 0])))
    events.append((BARS * BEATS_PER_BAR * ppq, 3, b"\xFF\x2F\x00"))
    events.sort(key=lambda event: (event[0], event[1]))
    track = bytearray()
    previous = 0
    for tick, _, message in events:
        track.extend(vlq(tick - previous))
        track.extend(message)
        previous = tick
    with path.open("wb") as handle:
        handle.write(b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq))
        handle.write(b"MTrk" + struct.pack(">I", len(track)) + track)


def oscillator(phase, instrument: str):
    sine = np.sin(phase)
    if instrument == "flute":
        return 0.86 * sine + 0.11 * np.sin(2 * phase) + 0.03 * np.sin(3 * phase)
    if instrument in ("bell", "chime"):
        return 0.58 * sine + 0.27 * np.sin(2.01 * phase) + 0.15 * np.sin(3.98 * phase)
    if instrument in ("harp", "lute"):
        return 0.68 * sine + 0.20 * np.sin(2 * phase) + 0.08 * np.sin(3 * phase) + 0.04 * np.sin(4 * phase)
    if instrument == "bass":
        return 0.78 * sine + 0.22 * np.sin(2 * phase)
    return 0.82 * sine + 0.12 * np.sin(2 * phase) + 0.06 * np.sin(3 * phase)


def render_note(note: Note, sample_rate: int) -> np.ndarray:
    duration = note.duration
    if note.instrument in ("harp", "lute", "bell", "chime"):
        duration += 1.5
    elif note.instrument == "strings":
        duration += 0.4
    count = max(1, round(duration * sample_rate))
    t = np.arange(count, dtype=np.float32) / sample_rate
    if note.instrument in ("drum", "snare", "hat"):
        seed = int(note.start * 1000) + note.pitch * 997
        rng = np.random.default_rng(seed)
        noise = rng.uniform(-1, 1, count).astype(np.float32)
        if note.instrument == "drum":
            phase = 2 * np.pi * (68 * t - 28 * t * t)
            signal = 0.76 * np.sin(phase) + 0.24 * noise
            envelope = np.exp(-t * 14)
        else:
            signal = noise
            envelope = np.exp(-t * (25 if note.instrument == "hat" else 16))
    else:
        frequency = 440.0 * 2 ** ((note.pitch - 69) / 12)
        phase = 2 * np.pi * frequency * t
        signal = oscillator(phase, note.instrument)
        attack = {"strings": 0.55, "flute": 0.12, "bass": 0.05}.get(note.instrument, 0.012)
        attack_env = np.minimum(1.0, t / attack)
        if note.instrument == "strings":
            release = np.clip((duration - t) / 0.8, 0, 1)
            envelope = attack_env * release * (0.93 + 0.07 * np.sin(2 * np.pi * 0.18 * t))
        elif note.instrument == "flute":
            release = np.clip((duration - t) / 0.22, 0, 1)
            vibrato = 1 + 0.015 * np.sin(2 * np.pi * 5.1 * t)
            envelope = attack_env * release * vibrato
        else:
            decay = {"harp": 2.2, "lute": 2.7, "bell": 1.25, "chime": 1.0, "bass": 1.3}.get(note.instrument, 2.0)
            envelope = attack_env * np.exp(-t * decay)
    mono = (signal * envelope * note.velocity).astype(np.float32)
    left = math.sqrt((1 - note.pan) * 0.5)
    right = math.sqrt((1 + note.pan) * 0.5)
    return np.column_stack((mono * left, mono * right))


def write_wav(path: Path, notes: list[Note], sample_rate: int = 22_050) -> None:
    if np is None:
        raise RuntimeError("NumPy is required for WAV rendering; use --skip-wav or run with bundled Python.")
    total_frames = DURATION_SECONDS * sample_rate
    chunk_frames = sample_rate * 10
    playback_notes: list[tuple[Note, float]] = [(note, 0.0) for note in notes]
    # Wrap the final harmony's release and hall reflections into the opening.
    # This makes the rendered waveform circular as well as the composition.
    for note in notes:
        tail = 1.5 if note.instrument in ("harp", "lute", "bell", "chime") else 0.4
        if note.start + note.duration + tail + 0.47 > DURATION_SECONDS:
            playback_notes.append((note, -DURATION_SECONDS))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        for chunk_start in range(0, total_frames, chunk_frames):
            chunk_end = min(total_frames, chunk_start + chunk_frames)
            audio = np.zeros((chunk_end - chunk_start, 2), dtype=np.float32)
            chunk_start_seconds = chunk_start / sample_rate
            chunk_end_seconds = chunk_end / sample_rate
            for note, time_shift in playback_notes:
                tail = 1.5 if note.instrument in ("harp", "lute", "bell", "chime") else 0.4
                shifted_start = note.start + time_shift
                if shifted_start > chunk_end_seconds or shifted_start + note.duration + tail + 0.47 < chunk_start_seconds:
                    continue
                note_start = round(shifted_start * sample_rate)
                sound = render_note(note, sample_rate)
                note_end = note_start + len(sound)
                if note_end <= chunk_start or note_start >= chunk_end:
                    continue
                src_start = max(0, chunk_start - note_start)
                src_end = min(len(sound), chunk_end - note_start)
                dst_start = max(0, note_start - chunk_start)
                audio[dst_start:dst_start + (src_end - src_start)] += sound[src_start:src_end]

                # Two quiet reflections give a spacious hall impression.
                for delay_seconds, gain, cross in ((0.23, 0.16, True), (0.47, 0.08, False)):
                    echo_start = note_start + round(delay_seconds * sample_rate)
                    echo_end = echo_start + len(sound)
                    if echo_end <= chunk_start or echo_start >= chunk_end:
                        continue
                    es = max(0, chunk_start - echo_start)
                    ee = min(len(sound), chunk_end - echo_start)
                    ds = max(0, echo_start - chunk_start)
                    echo = sound[es:ee, ::-1] if cross else sound[es:ee]
                    audio[ds:ds + (ee - es)] += echo * gain

            # Gentle bus saturation prevents rare stacked accents from clipping.
            audio = np.tanh(audio * 0.92) * 0.82
            pcm = (audio * 32767).astype("<i2")
            handle.writeframes(pcm.tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--skip-wav", action="store_true")
    parser.add_argument("--sample-rate", type=int, default=22_050)
    parser.add_argument("--variant", choices=("calm", "upbeat"), default="calm")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    stem_name, composer = configure_variant(args.variant)
    notes = composer()
    stem = args.output / stem_name
    write_nbs(stem.with_suffix(".nbs"), notes)
    write_midi(stem.with_suffix(".mid"), notes)
    if not args.skip_wav:
        write_wav(stem.with_suffix(".wav"), notes, args.sample_rate)
    print(f"Generated {len(notes):,} notes; duration {DURATION_SECONDS // 60}:00")


if __name__ == "__main__":
    main()

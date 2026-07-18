package dev.monkeycraft.lobbymusic;

import java.io.ByteArrayInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public final class NbsReader {
    private NbsReader() {
    }

    public static NbsSong read(InputStream input) throws IOException {
        LittleEndianInput data = new LittleEndianInput(input);
        int oldLength = data.unsignedShort();
        if (oldLength != 0) {
            throw new IOException("Only modern NBS files (version 4+) are supported");
        }
        int version = data.unsignedByte();
        if (version < 4 || version > 5) {
            throw new IOException("Unsupported NBS version: " + version);
        }
        data.unsignedByte(); // vanilla instrument count
        int lengthTicks = data.unsignedShort();
        data.unsignedShort(); // layer count

        String title = data.string();
        String author = data.string();
        data.string(); // original author
        String description = data.string();
        float ticksPerSecond = data.unsignedShort() / 100.0f;
        if (ticksPerSecond <= 0 || ticksPerSecond > 20) {
            throw new IOException("NBS tempo must be between 0 and 20 TPS");
        }

        data.skipFully(3); // autosave, autosave duration, time signature
        data.skipFully(20); // editor statistics
        data.string(); // source MIDI/Schematic file
        data.skipFully(4); // looping enabled, max loops, loop start tick

        Map<Integer, List<NbsNote>> notes = new HashMap<>();
        int tick = -1;
        while (true) {
            int tickJump = data.unsignedShort();
            if (tickJump == 0) {
                break;
            }
            tick += tickJump;
            int layer = -1;
            while (true) {
                int layerJump = data.unsignedShort();
                if (layerJump == 0) {
                    break;
                }
                layer += layerJump;
                int instrument = data.unsignedByte();
                int key = data.unsignedByte();
                int velocity = data.unsignedByte();
                data.unsignedByte(); // stereo panning; radio playback is player-local
                data.signedShort(); // fine pitch
                if (instrument < 16 && key >= 33 && key <= 57 && velocity > 0) {
                    notes.computeIfAbsent(tick, ignored -> new ArrayList<>())
                            .add(new NbsNote(instrument, key, velocity));
                }
            }
        }

        Map<Integer, List<NbsNote>> immutableNotes = new HashMap<>();
        notes.forEach((key, value) -> immutableNotes.put(key, List.copyOf(value)));
        return new NbsSong(title, author, description, lengthTicks, ticksPerSecond,
                Map.copyOf(immutableNotes));
    }

    public static NbsSong read(byte[] bytes) throws IOException {
        return read(new ByteArrayInputStream(bytes));
    }

    private static final class LittleEndianInput {
        private final InputStream input;

        private LittleEndianInput(InputStream input) {
            this.input = input;
        }

        private int unsignedByte() throws IOException {
            int value = input.read();
            if (value < 0) {
                throw new EOFException("Unexpected end of NBS file");
            }
            return value;
        }

        private int unsignedShort() throws IOException {
            return unsignedByte() | (unsignedByte() << 8);
        }

        private int signedShort() throws IOException {
            int value = unsignedShort();
            return value >= 0x8000 ? value - 0x10000 : value;
        }

        private long unsignedInt() throws IOException {
            return (long) unsignedByte()
                    | ((long) unsignedByte() << 8)
                    | ((long) unsignedByte() << 16)
                    | ((long) unsignedByte() << 24);
        }

        private String string() throws IOException {
            long length = unsignedInt();
            if (length > 1_048_576) {
                throw new IOException("NBS string is unreasonably large");
            }
            byte[] bytes = input.readNBytes((int) length);
            if (bytes.length != length) {
                throw new EOFException("Unexpected end of NBS string");
            }
            return new String(bytes, StandardCharsets.UTF_8);
        }

        private void skipFully(int count) throws IOException {
            if (input.readNBytes(count).length != count) {
                throw new EOFException("Unexpected end of NBS header");
            }
        }
    }
}

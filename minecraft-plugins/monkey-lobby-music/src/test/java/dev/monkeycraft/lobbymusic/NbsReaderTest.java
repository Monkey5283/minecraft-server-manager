package dev.monkeycraft.lobbymusic;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class NbsReaderTest {
    private static final Path AUDIO = Path.of("../../minecraft-audio/monkeycraft-lobby");

    @Test
    void calmTrackIsFifteenMinutesAndUsesVanillaPitchRange() throws IOException {
        assertTrack("monkeycraft_nexus_awaits.nbs", "The Nexus Awaits");
    }

    @Test
    void upbeatTrackIsFifteenMinutesAndUsesVanillaPitchRange() throws IOException {
        assertTrack("monkeycraft_festival_of_the_skyways.nbs", "Festival of the Skyways");
    }

    private static void assertTrack(String filename, String title) throws IOException {
        NbsSong song = NbsReader.read(Files.readAllBytes(AUDIO.resolve(filename)));
        assertEquals(title, song.title());
        assertEquals(9000, song.lengthTicks());
        assertEquals(10.0f, song.ticksPerSecond());
        assertFalse(song.notesByTick().isEmpty());
        assertTrue(song.notesByTick().values().stream().flatMap(java.util.Collection::stream)
                .allMatch(note -> note.key() >= 33 && note.key() <= 57));
    }
}

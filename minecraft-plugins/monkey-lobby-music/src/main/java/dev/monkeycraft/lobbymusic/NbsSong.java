package dev.monkeycraft.lobbymusic;

import java.util.List;
import java.util.Map;

public record NbsSong(
        String title,
        String author,
        String description,
        int lengthTicks,
        float ticksPerSecond,
        Map<Integer, List<NbsNote>> notesByTick
) {
}

record NbsNote(int instrument, int key, int velocity) {
}

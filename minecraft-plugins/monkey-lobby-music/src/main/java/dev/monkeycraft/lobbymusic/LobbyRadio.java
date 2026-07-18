package dev.monkeycraft.lobbymusic;

import org.bukkit.Sound;
import org.bukkit.SoundCategory;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

public final class LobbyRadio {
    private static final Sound[] INSTRUMENTS = {
            Sound.BLOCK_NOTE_BLOCK_HARP,
            Sound.BLOCK_NOTE_BLOCK_BASS,
            Sound.BLOCK_NOTE_BLOCK_BASEDRUM,
            Sound.BLOCK_NOTE_BLOCK_SNARE,
            Sound.BLOCK_NOTE_BLOCK_HAT,
            Sound.BLOCK_NOTE_BLOCK_GUITAR,
            Sound.BLOCK_NOTE_BLOCK_FLUTE,
            Sound.BLOCK_NOTE_BLOCK_BELL,
            Sound.BLOCK_NOTE_BLOCK_CHIME,
            Sound.BLOCK_NOTE_BLOCK_XYLOPHONE,
            Sound.BLOCK_NOTE_BLOCK_IRON_XYLOPHONE,
            Sound.BLOCK_NOTE_BLOCK_COW_BELL,
            Sound.BLOCK_NOTE_BLOCK_DIDGERIDOO,
            Sound.BLOCK_NOTE_BLOCK_BIT,
            Sound.BLOCK_NOTE_BLOCK_BANJO,
            Sound.BLOCK_NOTE_BLOCK_PLING
    };

    private final JavaPlugin plugin;
    private final List<NbsSong> songs;
    private final Map<UUID, Float> playerVolumes = new HashMap<>();
    private final float masterVolume;
    private final int gapServerTicks;
    private final boolean announceTracks;
    private BukkitTask task;
    private int songIndex;
    private int songTick;
    private int gapRemaining;
    private double tickAccumulator;

    public LobbyRadio(JavaPlugin plugin, List<NbsSong> songs, float masterVolume,
               int gapSeconds, boolean announceTracks) {
        this.plugin = plugin;
        this.songs = List.copyOf(songs);
        this.masterVolume = Math.max(0.0f, Math.min(1.0f, masterVolume));
        this.gapServerTicks = Math.max(0, gapSeconds * 20);
        this.announceTracks = announceTracks;
    }

    public void start() {
        if (task != null) {
            return;
        }
        announceCurrentSong();
        task = plugin.getServer().getScheduler().runTaskTimer(plugin, this::serverTick, 1L, 1L);
    }

    public void stop() {
        if (task != null) {
            task.cancel();
            task = null;
        }
    }

    void skip() {
        advanceSong();
    }

    NbsSong currentSong() {
        return songs.get(songIndex);
    }

    void toggle(Player player) {
        float current = playerVolumes.getOrDefault(player.getUniqueId(), 1.0f);
        playerVolumes.put(player.getUniqueId(), current > 0 ? 0.0f : 1.0f);
    }

    boolean isMuted(Player player) {
        return playerVolumes.getOrDefault(player.getUniqueId(), 1.0f) == 0.0f;
    }

    void setPlayerVolume(Player player, int percent) {
        playerVolumes.put(player.getUniqueId(), Math.max(0, Math.min(100, percent)) / 100.0f);
    }

    int playerVolume(Player player) {
        return Math.round(playerVolumes.getOrDefault(player.getUniqueId(), 1.0f) * 100);
    }

    private void serverTick() {
        if (gapRemaining > 0) {
            gapRemaining--;
            if (gapRemaining == 0) {
                advanceSong();
            }
            return;
        }

        NbsSong song = currentSong();
        tickAccumulator += song.ticksPerSecond() / 20.0;
        while (tickAccumulator >= 1.0) {
            playNotes(song.notesByTick().get(songTick));
            songTick++;
            tickAccumulator -= 1.0;
            if (songTick >= song.lengthTicks()) {
                songTick = song.lengthTicks();
                gapRemaining = Math.max(1, gapServerTicks);
                tickAccumulator = 0;
                return;
            }
        }
    }

    private void playNotes(List<NbsNote> notes) {
        if (notes == null || notes.isEmpty()) {
            return;
        }
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            float personalVolume = playerVolumes.getOrDefault(player.getUniqueId(), 1.0f);
            if (personalVolume <= 0) {
                continue;
            }
            for (NbsNote note : notes) {
                float volume = masterVolume * personalVolume * note.velocity() / 100.0f;
                float pitch = (float) Math.pow(2.0, (note.key() - 45) / 12.0);
                player.playSound(player.getLocation(), INSTRUMENTS[note.instrument()],
                        SoundCategory.RECORDS, volume, pitch);
            }
        }
    }

    private void advanceSong() {
        songIndex = (songIndex + 1) % songs.size();
        songTick = 0;
        gapRemaining = 0;
        tickAccumulator = 0;
        announceCurrentSong();
    }

    private void announceCurrentSong() {
        if (!announceTracks) {
            return;
        }
        String message = "§6♫ §eNow playing: §f" + currentSong().title();
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            if (!isMuted(player)) {
                player.sendMessage(message);
            }
        }
    }
}

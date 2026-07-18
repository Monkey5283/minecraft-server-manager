package dev.monkeycraft.lobbymusic;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

public final class MonkeyLobbyMusicPlugin extends JavaPlugin {
    private LobbyRadio radio;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        try {
            List<NbsSong> songs = loadSongs(getConfig().getStringList("songs"));
            if (songs.isEmpty()) {
                throw new IOException("No lobby songs were configured");
            }
            radio = new LobbyRadio(
                    this,
                    songs,
                    (float) getConfig().getDouble("volume", 0.55),
                    getConfig().getInt("gap-seconds", 5),
                    getConfig().getBoolean("announce-tracks", true)
            );
            radio.start();
            getLogger().info("Lobby radio started with " + songs.size() + " songs: "
                    + songs.stream().map(NbsSong::title).toList());
        } catch (IOException exception) {
            getLogger().severe("Could not load lobby music: " + exception.getMessage());
            getServer().getPluginManager().disablePlugin(this);
        }
    }

    @Override
    public void onDisable() {
        if (radio != null) {
            radio.stop();
        }
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (radio == null) {
            sender.sendMessage("§cThe lobby radio is unavailable.");
            return true;
        }
        if (args.length == 0 || args[0].equalsIgnoreCase("now")) {
            sender.sendMessage("§6♫ §eNow playing: §f" + radio.currentSong().title());
            return true;
        }
        if (args[0].equalsIgnoreCase("skip")) {
            if (!sender.hasPermission("monkeylobbymusic.skip")) {
                sender.sendMessage("§cYou do not have permission to skip the lobby radio.");
                return true;
            }
            radio.skip();
            sender.sendMessage("§aSkipped to §f" + radio.currentSong().title());
            return true;
        }
        if (!(sender instanceof Player player)) {
            sender.sendMessage("§cThat command is only available to players.");
            return true;
        }
        if (!sender.hasPermission("monkeylobbymusic.use")) {
            sender.sendMessage("§cYou do not have permission to control lobby music.");
            return true;
        }
        if (args[0].equalsIgnoreCase("toggle")) {
            radio.toggle(player);
            sender.sendMessage(radio.isMuted(player)
                    ? "§7Lobby music muted. Use §f/radio toggle §7to restore it."
                    : "§aLobby music enabled at 100%. Use §f/radio volume <0-100>§a to adjust it.");
            return true;
        }
        if (args[0].equalsIgnoreCase("volume")) {
            if (args.length == 1) {
                sender.sendMessage("§eLobby music volume: §f" + radio.playerVolume(player) + "%");
                return true;
            }
            try {
                int percent = Integer.parseInt(args[1]);
                if (percent < 0 || percent > 100) {
                    throw new NumberFormatException();
                }
                radio.setPlayerVolume(player, percent);
                sender.sendMessage("§aLobby music volume set to §f" + percent + "%§a.");
            } catch (NumberFormatException exception) {
                sender.sendMessage("§cVolume must be a whole number from 0 to 100.");
            }
            return true;
        }
        sender.sendMessage("§e/radio now§7, §e/radio toggle§7, §e/radio volume <0-100>§7, §e/radio skip");
        return true;
    }

    private List<NbsSong> loadSongs(List<String> resources) throws IOException {
        List<NbsSong> songs = new ArrayList<>();
        for (String resource : resources) {
            try (InputStream input = getResource(resource)) {
                if (input == null) {
                    throw new IOException("Missing embedded song: " + resource);
                }
                songs.add(NbsReader.read(input));
            }
        }
        return List.copyOf(songs);
    }
}

package dev.monkeycraft.portals;

import dev.monkeycraft.lobbymusic.LobbyRadio;
import dev.monkeycraft.lobbymusic.NbsReader;
import dev.monkeycraft.lobbymusic.NbsSong;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import org.bukkit.ChatColor;
import org.bukkit.Location;
import org.bukkit.command.Command;
import org.bukkit.command.PluginCommand;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

public final class MonkeyPortalsPlugin extends JavaPlugin {
    private PortalRepository portals;
    private PortalListener portalListener;
    private String localServer = "";
    private List<String> knownServers = List.of("lobby", "vanilla");
    private long transferCooldownMillis = 2000;
    private long joinGraceMillis = 3000;
    private boolean logTransfers = true;
    private volatile Location arrivalSpawn;
    private LobbyRadio lobbyRadio;

    @Override
    public void onEnable() {
        retireStandaloneMusicJars();
        saveDefaultConfig();
        portals = new PortalRepository(this);
        portalListener = new PortalListener(this);
        reloadPluginState();

        getServer().getMessenger().registerOutgoingPluginChannel(this, "BungeeCord");
        getServer().getPluginManager().registerEvents(portalListener, this);

        PortalCommand commandHandler = new PortalCommand(this);
        PluginCommand command = Objects.requireNonNull(getCommand("mportal"), "mportal command");
        command.setExecutor(commandHandler);
        command.setTabCompleter(commandHandler);

        PluginCommand radioCommand = Objects.requireNonNull(
                getCommand("lobbymusic"), "lobbymusic command");
        radioCommand.setExecutor(this::handleRadioCommand);
        startLobbyRadio();

        getLogger().info("Loaded " + portals.all().size()
                + " portal(s). Players transfer or teleport by walking into regions; no item is used.");
    }

    @Override
    public void onDisable() {
        if (lobbyRadio != null) {
            lobbyRadio.stop();
        }
        getServer().getMessenger().unregisterOutgoingPluginChannel(this);
        if (portalListener != null) {
            portalListener.clearState();
        }
    }

    private void startLobbyRadio() {
        if (!getConfig().getBoolean("lobby-radio.enabled", true)) {
            return;
        }
        List<String> resources = getConfig().getStringList("lobby-radio.songs");
        if (resources.isEmpty()) {
            resources = List.of(
                    "songs/monkeycraft_nexus_awaits.nbs",
                    "songs/monkeycraft_festival_of_the_skyways.nbs"
            );
        }
        try {
            List<NbsSong> songs = loadEmbeddedLobbySongs(resources);
            lobbyRadio = new LobbyRadio(
                    this,
                    songs,
                    (float) getConfig().getDouble("lobby-radio.volume", 0.55),
                    getConfig().getInt("lobby-radio.gap-seconds", 5),
                    getConfig().getBoolean("lobby-radio.announce-tracks", true)
            );
            lobbyRadio.start();
            getLogger().info("Lobby radio started with " + songs.size() + " songs: "
                    + songs.stream().map(NbsSong::title).toList());
        } catch (IOException exception) {
            getLogger().severe("Could not load lobby music: " + exception.getMessage());
        }
    }

    private List<NbsSong> loadEmbeddedLobbySongs(List<String> resources) throws IOException {
        List<NbsSong> songs = new ArrayList<>();
        for (String resource : resources) {
            try (InputStream input = getResource(resource)) {
                if (input == null) {
                    throw new IOException("Missing embedded song: " + resource);
                }
                songs.add(NbsReader.read(input));
            }
        }
        if (songs.isEmpty()) {
            throw new IOException("No lobby songs were configured");
        }
        return List.copyOf(songs);
    }

    private boolean handleRadioCommand(
            CommandSender sender, Command command, String label, String[] args) {
        if (lobbyRadio == null) {
            sender.sendMessage(ChatColor.RED + "The lobby radio is unavailable.");
            return true;
        }
        if (args.length == 0 || args[0].equalsIgnoreCase("now")) {
            sender.sendMessage(ChatColor.GOLD + "Now playing: " + ChatColor.WHITE
                    + lobbyRadio.currentSong().title());
            return true;
        }
        if (args[0].equalsIgnoreCase("skip")) {
            if (!sender.hasPermission("monkeylobbymusic.skip")) {
                sender.sendMessage(ChatColor.RED + "You do not have permission to skip the lobby radio.");
                return true;
            }
            lobbyRadio.skip();
            sender.sendMessage(ChatColor.GREEN + "Skipped to " + ChatColor.WHITE
                    + lobbyRadio.currentSong().title());
            return true;
        }
        if (!(sender instanceof Player player)) {
            sender.sendMessage(ChatColor.RED + "That command is only available to players.");
            return true;
        }
        if (!sender.hasPermission("monkeylobbymusic.use")) {
            sender.sendMessage(ChatColor.RED + "You do not have permission to control lobby music.");
            return true;
        }
        if (args[0].equalsIgnoreCase("toggle")) {
            lobbyRadio.toggle(player);
            sender.sendMessage(lobbyRadio.isMuted(player)
                    ? ChatColor.GRAY + "Lobby music muted. Use /radio toggle to restore it."
                    : ChatColor.GREEN + "Lobby music enabled. Use /radio volume <0-100> to adjust it.");
            return true;
        }
        if (args[0].equalsIgnoreCase("volume")) {
            if (args.length == 1) {
                sender.sendMessage(ChatColor.YELLOW + "Lobby music volume: " + ChatColor.WHITE
                        + lobbyRadio.playerVolume(player) + "%");
                return true;
            }
            try {
                int percent = Integer.parseInt(args[1]);
                if (percent < 0 || percent > 100) {
                    throw new NumberFormatException();
                }
                lobbyRadio.setPlayerVolume(player, percent);
                sender.sendMessage(ChatColor.GREEN + "Lobby music volume set to " + ChatColor.WHITE
                        + percent + "%" + ChatColor.GREEN + ".");
            } catch (NumberFormatException exception) {
                sender.sendMessage(ChatColor.RED + "Volume must be a whole number from 0 to 100.");
            }
            return true;
        }
        sender.sendMessage(ChatColor.YELLOW
                + "/radio now, /radio toggle, /radio volume <0-100>, /radio skip");
        return true;
    }

    private void retireStandaloneMusicJars() {
        Path pluginsDirectory = getDataFolder().toPath().getParent();
        if (pluginsDirectory == null) {
            return;
        }
        for (String name : List.of("MonkeyLobbyMusic.jar", "MonkeyLobbyMusic-1.0.0.jar")) {
            Path staleJar = pluginsDirectory.resolve(name);
            try {
                if (Files.deleteIfExists(staleJar)) {
                    getLogger().info("Retired stale standalone music JAR: " + name);
                }
            } catch (IOException exception) {
                getLogger().warning("Could not retire stale standalone music JAR " + name
                        + ": " + exception.getMessage());
            }
        }
    }

    void reloadPluginState() {
        reloadConfig();
        localServer = getConfig().getString("local-server", "").trim();
        knownServers = getConfig().getStringList("known-servers").stream()
                .filter(PortalRegion::isValidServer)
                .distinct()
                .toList();
        transferCooldownMillis = Math.max(0, getConfig().getLong("transfer-cooldown-ms", 2000));
        joinGraceMillis = Math.max(0, getConfig().getLong("join-grace-ms", 3000));
        logTransfers = getConfig().getBoolean("log-transfers", true);
        Location configuredArrival = getConfig().getLocation("arrival-spawn.location");
        if (getConfig().getBoolean("arrival-spawn.enabled", false)
                && configuredArrival != null
                && configuredArrival.getWorld() != null) {
            arrivalSpawn = configuredArrival.clone();
        } else {
            arrivalSpawn = null;
        }
        portals.load();
        if (portalListener != null) {
            portalListener.clearState();
        }
    }

    PortalRepository portals() {
        return portals;
    }

    String localServer() {
        return localServer;
    }

    List<String> knownServers() {
        return knownServers;
    }

    long transferCooldownMillis() {
        return transferCooldownMillis;
    }

    long joinGraceMillis() {
        return joinGraceMillis;
    }

    boolean logTransfers() {
        return logTransfers;
    }

    Location arrivalSpawn() {
        Location configured = arrivalSpawn;
        return configured == null ? null : configured.clone();
    }

    void setArrivalSpawn(Location location) {
        Location saved = location.clone();
        getConfig().set("arrival-spawn.enabled", true);
        getConfig().set("arrival-spawn.location", saved);
        saveConfig();
        arrivalSpawn = saved;
    }

    void clearArrivalSpawn() {
        getConfig().set("arrival-spawn.enabled", false);
        getConfig().set("arrival-spawn.location", null);
        saveConfig();
        arrivalSpawn = null;
    }

    void sendConfiguredMessage(CommandSender recipient, String path, String destination) {
        String message = getConfig().getString(path, "");
        if (message == null || message.isBlank()) {
            return;
        }
        String prefix = getConfig().getString("messages.prefix", "");
        String formatted = (prefix == null ? "" : prefix) + message
                .replace("{server}", destination)
                .replace("{destination}", destination);
        recipient.sendMessage(ChatColor.translateAlternateColorCodes('&', formatted));
    }

}

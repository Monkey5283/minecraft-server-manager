# Network plugins

This setup restores Bedrock cross-play and adds private, inventory-free server
portals to the Paper backends. It matches the network documented in the main
README:

- Velocity and the Lobby Paper server run on `192.168.1.35`.
- the Vanilla Paper server runs on `192.168.1.16`;
- the Velocity server IDs are `lobby` and `vanilla` (the manager still calls
  the Vanilla entry `survival`);
- the Forge 1.20.1 backend is `civilizations` at `192.168.1.175:25580`.

The managed profiles install:

| Profile | Server | Plugins |
| --- | --- | --- |
| `velocity-crossplay` | Velocity | Geyser-Velocity, Floodgate-Velocity, and the MCXboxBroadcast Geyser extension |
| `lobby-network` | Lobby Paper | ViaVersion, ViaBackwards, MonkeyPortals, and MonkeyLobbyMusic |
| `paper-network` | Vanilla Paper | ViaVersion, ViaBackwards, and MonkeyPortals |

Geyser, Floodgate, and MCXboxBroadcast belong on Velocity. MCXboxBroadcast
publishes the existing Geyser listener as a joinable Xbox friend session for
Switch, Switch 2, Xbox, and PlayStation players. ViaVersion belongs on every
Paper backend so the Java protocol emulated by current Geyser builds can reach
an older backend; ViaBackwards retains support for older Java clients.
MonkeyPortals is the network's private Paper plugin. An admin marks two corners
and assigns the cuboid to a Velocity server, an exact local location, a local
world spawn, or another portal region. Players travel when they walk inside the
region, without receiving or carrying a selector item.

The updater resolves public releases from the official Geyser download API,
Modrinth, and the official MCXboxBroadcast GitHub releases. Geyser and GitHub
release artifacts are SHA-256 verified, Modrinth artifacts are SHA-512
verified, and every artifact is checked as a readable JAR. MonkeyPortals is not
published: the updater takes it only from the root-owned private artifact at
`/usr/local/share/minecraft-manager/MonkeyPortals.jar`. Existing configs and
MCXboxBroadcast authentication state are preserved, and old JARs are backed up
before the relevant service is restarted.

Use a current stable Velocity release before installing current Geyser builds.
The deployed network uses Velocity 4.0.0; Geyser 2.11.0 does not start correctly
against the older Adventure libraries bundled with Velocity 3.5.1.

The Lobby profile also installs the private, dependency-free MonkeyLobbyMusic
server radio. It
rotates the original 15-minute `The Nexus Awaits` and `Festival of the Skyways`
NBS tracks with a five-second pause, then repeats. The tracks use vanilla note
block sounds and do not require a client resource pack. Players can use
`/radio volume <0-100>` or `/radio toggle` to control their own playback, and
operators can use `/radio skip`.

## 1. Install the updater

Update the manager on both Minecraft hosts:

```bash
sudo update-minecraft-manager
```

This installs `/usr/local/sbin/update-minecraft-plugins`. It does not install or
update a plugin until a profile is run.

## 2. Build and stage the private portal plugin

Build from the private working copy with Java 25:

```bash
cd minecraft-plugins/monkey-portals
./gradlew clean test jar
```

Copy `build/libs/MonkeyPortals-1.3.0.jar` privately to each Paper host, then
stage it with root ownership:

```bash
sudo install -o root -g root -m 0644 MonkeyPortals-1.3.0.jar \
  /usr/local/share/minecraft-manager/MonkeyPortals.jar
```

Do not upload this artifact to Modrinth, Hangar, GitHub Releases, or another
public plugin registry. The managed updater refuses a staged artifact that is
not root-owned or is writable by group/other.

## 3. Describe each server to the updater

On the Velocity/Lobby machine:

```bash
cd /opt/minecraft-manager/source
sudo install -d -o root -g root -m 0755 /etc/minecraft
sudo install -o root -g root -m 0644 config/plugins.velocity.example.env \
  /etc/minecraft/velocity-plugins.env
sudo install -o root -g root -m 0644 config/plugins.lobby.example.env \
  /etc/minecraft/lobby-plugins.env
```

On the Vanilla machine:

```bash
cd /opt/minecraft-manager/source
sudo install -d -o root -g root -m 0755 /etc/minecraft
sudo install -o root -g root -m 0644 config/plugins.survival.example.env \
  /etc/minecraft/survival-plugins.env
```

Open the installed files and confirm `SERVER_DIR`, `SERVICE`, `OWNER`, and
`GROUP` match the real service. In `velocity-plugins.env`, keep
`MCXBOX_BROADCAST_ADDRESS=monkeycraft.monkeyservers.net` and
`MCXBOX_BROADCAST_PORT=20210`: those are the public TCPShield Bedrock endpoint,
not Geyser's protected origin listener. The supplied Vanilla example uses
`/home/monkeycraftvanilla/Vanilla` and `papermc.service`; change it if that
machine has moved.

## 4. Allow the manager agent to run only these profiles

On the Velocity/Lobby host, add these exact lines to
`/etc/sudoers.d/minecraft-manager`:

```sudoers
mcmanager ALL=(root) NOPASSWD: /usr/local/sbin/update-minecraft-plugins velocity-crossplay velocity
mcmanager ALL=(root) NOPASSWD: /usr/local/sbin/update-minecraft-plugins lobby-network lobby
```

On the Vanilla host, add:

```sudoers
mcmanager ALL=(root) NOPASSWD: /usr/local/sbin/update-minecraft-plugins paper-network survival
```

Validate each edited file before using it:

```bash
sudo visudo -cf /etc/sudoers.d/minecraft-manager
```

In `/etc/minecraft-manager/agent.toml`, add the matching script inside each
existing server block. Do not create duplicate `[[servers]]` entries.

Velocity:

```toml
[servers.scripts]
update_plugins = [["sudo", "-n", "/usr/local/sbin/update-minecraft-plugins", "velocity-crossplay", "velocity"]]
```

Lobby:

```toml
[servers.scripts]
update_plugins = [["sudo", "-n", "/usr/local/sbin/update-minecraft-plugins", "lobby-network", "lobby"]]
```

Vanilla:

```toml
[servers.scripts]
update_plugins = [["sudo", "-n", "/usr/local/sbin/update-minecraft-plugins", "paper-network", "survival"]]
```

If one of those blocks already has scripts such as `backup` or
`shutdown_host`, add only the `update_plugins` line under the existing header.
Restart the agent after editing:

```bash
sudo systemctl restart mc-manager-agent
```

## 5. Install the backend plugins first

On the Lobby host:

```bash
sudo update-minecraft-plugins lobby-network lobby
```

On the Vanilla host:

```bash
sudo update-minecraft-plugins paper-network survival
```

The Lobby profile retires `ServerSelector.jar` into the timestamped plugin
backup and installs MonkeyPortals, so players no longer receive the locked
compass. The private plugin is installed on both backends because admins may
want a portal in either direction.

After the first start, set `local-server` in each generated
`plugins/MonkeyPortals/config.yml`:

```yaml
# Lobby
local-server: lobby

# Vanilla
local-server: vanilla
```

The `known-servers` list defaults to `lobby`, `vanilla`, and `civilizations`. These values must
match the case-sensitive names under `[servers]` in `velocity.toml`. Restart the
backend or run `/mportal reload` after editing the plugin config.

### Civilizations routing through the normal address

Civilizations players use the normal Monkeycraft address. Their private
modpack includes `MonkeycraftNetworkCompat`, which replaces only the virtual
host in the Minecraft handshake. Velocity sees the existing Civilizations
forced-host name and routes the connection directly to the Forge 1.20.1
backend. The player's saved address and TCP destination remain the normal
Monkeycraft address.

Do not route this modpack through the newer Paper Lobby. Vanilla block states
and dynamic registry data from that backend are not compatible with the large
Forge 1.20.1 pack. Also do not bundle Forge Client Reset Packet: direct routing
does not need a backend registry reset, and its network mixin conflicts with
the pack's Connectivity mod.

Build the private compatibility JAR from
`minecraft-mods/monkeycraft-network-compat`. Keep it private and distribute it
only inside the Civilizations modpack. The JAR is client-only and does not
belong on a backend server.

In `velocity.toml`, keep Bungee-compatible plugin messaging enabled so the
portal plugin can ask Velocity to transfer a player:

```toml
bungee-plugin-message-channel = true
```

### Create a portal

Build any visible frame or doorway, then stand at the two opposite corners of
the volume players will walk through:

```text
/mportal pos1
/mportal pos2
/mportal create vanilla_gate vanilla
```

On Vanilla, use the same workflow with `lobby` as the destination. Other admin
commands are `/mportal list`, `/mportal info <name>`,
`/mportal setserver <name> <server>`, `/mportal remove <name>`, and
`/mportal reload`. Regions persist in `plugins/MonkeyPortals/portals.yml`.

MonkeyPortals 1.3 also supports destinations inside one Paper instance. Keep
the `/mportal pos1` and `/mportal pos2` selection, then use one of:

```text
# Capture your exact current location and facing after leaving the selection
/mportal create market_gate location

# Use a loaded world's configured spawn
/mportal create nether_gate world world_nether

# Arrive at the center of an existing MonkeyPortals region
/mportal create return_gate portal market_gate
```

Existing Velocity syntax remains valid; the explicit equivalent is
`/mportal create vanilla_gate server vanilla`. Change an existing destination
with `/mportal setlocation <name>`, `/mportal setworld <name> [world]`, or
`/mportal setportal <name> <destination-portal>`. Legacy `portals.yml` entries
with a direct `server:` key are upgraded automatically when next saved.

To keep incoming Lobby players out of a portal frame, stand at the desired safe
arrival point and run `/mportal setspawn`. The exact position and facing are
stored in `plugins/MonkeyPortals/config.yml`. Use `/mportal spawn` to inspect it
or `/mportal clearspawn` to disable it. Arrival spawns are per backend and are
disabled by default.

## 6. Install and configure cross-play on Velocity

Run:

```bash
sudo update-minecraft-plugins velocity-crossplay velocity
```

The profile installs Geyser and Floodgate in Velocity's `plugins` directory and
MCXboxBroadcast at
`plugins/Geyser-Velocity/extensions/MCXboxBroadcastExtension.jar`. It seeds the
extension's first config with the public address and port from
`/etc/minecraft/velocity-plugins.env`; later updates preserve that config and
the extension's authentication state.

On its first start, Geyser and Floodgate generate their configuration folders.
Edit the Geyser Velocity `config.yml` under the Velocity `plugins` directory
and set:

```yaml
bedrock:
  address: 0.0.0.0
  port: 20210
  clone-remote-port: false
```

Also change Geyser's `auth-type` value to `floodgate`. Keep its generated key
material private and out of Git.

In `velocity.toml`, Bedrock chat compatibility also requires:

```toml
force-key-authentication = false
```

Restart Velocity after those edits:

```bash
sudo systemctl restart velocity.service
```

### Create and authenticate the console join account

The production console join account is a dedicated Microsoft account with the
Xbox gamertag `Monkeycraft5823`. The shorter `Monkeycraft` gamertag was not
available. Do not use a personal, staff, server-owner, or Minecraft
administrator account: MCXboxBroadcast is an unofficial client emulator and
its upstream project explicitly recommends a separate account.

Watch the first Velocity start for the short-lived Microsoft device code:

```bash
sudo journalctl -u velocity.service -f
```

Open <https://www.microsoft.com/link>, enter the displayed code, and sign in
with only the dedicated `Monkeycraft5823` account. The server manager never
stores the Microsoft password. MCXboxBroadcast stores its refreshable session
files under `plugins/Geyser-Velocity/extensions/mcxboxbroadcast`; keep that
directory private and include it in normal server backups. If the code expires,
run `mcxboxbroadcast restart` in the Velocity console to generate another one.

Players then add `Monkeycraft5823` as an Xbox/Minecraft friend. Automatic follow
sync is enabled, so the server appears under **Play > Friends > Joinable
Friends**. Players still need their console's online subscription, a signed-in
Microsoft account, and multiplayer permissions; no DNS change or mobile app is
needed.

Allow Geyser's UDP port through UFW only from the intended ingress source. For
the hidden-origin UniFi deployment, use the TCPShield-specific CIDRs in
[`unifi-monkeycraft-vlan.md`](unifi-monkeycraft-vlan.md). Do not use a broad
`sudo ufw allow 20210/udp` rule in that deployment.

If migrating from the previous port, remove its obsolete firewall allowance:

```bash
sudo ufw delete allow 19132/udp
```

For players outside the LAN, UDP `20210` must reach the Geyser listener on the
Velocity host. Do not point it at a Paper backend, and do not reuse either
backend Query port. When hiding the origin behind TCPShield, use the
source-restricted forward and public Bedrock endpoint documented in
[`unifi-monkeycraft-vlan.md`](unifi-monkeycraft-vlan.md); do not create an
unrestricted direct WAN forward.

Floodgate is required only on Velocity for normal authentication. Install it on
the Paper backends only if a backend plugin needs the Floodgate API; doing that
also requires secure key distribution and `send-floodgate-data`.

If this network is being migrated from a direct Paper Geyser setup, archive the
backend `Geyser-Spigot.jar` and `floodgate-spigot.jar` after proxy cross-play is
working. Preserve their data folders until the migration has been verified.

## 7. Verify

1. Join through the normal Java address and confirm no selector item is added to
   the inventory.
2. Walk into a Lobby portal and confirm Velocity transfers the player to
   Vanilla. Test a return portal separately if one has been configured.
3. Join from Bedrock using the same public host. In the deployed hidden-origin
   configuration, TCPShield accepts public UDP `20210` and relays it to
   Geyser's private origin port `20210`.
4. Run `geyser extensions` in the Velocity console and confirm
   `MCXboxBroadcast` is loaded. Follow `Monkeycraft5823` from a test Microsoft
   account, confirm the account follows back, and join the advertised session
   from a console's Friends tab.
5. For a direct legacy deployment only, run
   `geyser connectiontest PUBLIC_IP 20210` from the Velocity console if an
   external Bedrock connection cannot reach the server. Do not temporarily
   expose the origin to run this test in the hidden-origin deployment.

Plugin backups are stored below each server's
`plugins/.minecraft-manager-backups/` directory. Existing Geyser, Floodgate,
MCXboxBroadcast, ViaVersion, ServerSelector, and MonkeyPortals data folders are
never overwritten by the updater.

# Minecraft Server Manager

Control Ubuntu Minecraft servers from Discord or a private web dashboard.

The Raspberry Pi runs the controller. Each Ubuntu Minecraft machine runs a
small agent that can only execute commands listed in its local configuration.

## What you need

- Raspberry Pi with Raspberry Pi OS Lite 64-bit
- Ubuntu machines running the Minecraft servers
- A Discord bot token
- Tailscale on the Pi and on devices that may open the dashboard
- Fixed LAN addresses or DHCP reservations for the Pi and Ubuntu machines
- Optional: a USB UPS on the Pi, configured with NUT

Do not forward the dashboard or agent ports through your router.

## 1. Install the controller on the Pi

Run:

```bash
sudo apt update
sudo apt install -y git
cd ~
git clone https://github.com/Monkey5283/minecraft-server-manager.git

sudo bash ~/minecraft-server-manager/deploy/scripts/bootstrap-minecraft-manager \
  controller https://github.com/Monkey5283/minecraft-server-manager.git main
```

The installer creates configuration files without starting the controller.

## 2. Configure the controller

Open the main configuration:

```bash
sudo nano /etc/minecraft-manager/controller.toml
```

For every Minecraft server, set:

- `id`: short lowercase name such as `survival`
- `name`: name displayed in Discord and the dashboard
- `agent_url`: Ubuntu machine's LAN address, such as
  `http://192.168.1.31:8766`
- `token_env`: unique environment-variable name for that machine

Remove example server entries you do not need.

Under `[discord]`, set `announcement_channel_id` to the Discord text channel
that should receive a message whenever the controller comes online. Enable
Discord Developer Mode, right-click the channel, and choose **Copy Channel ID**.
Set it to `0` to disable announcements.

The bot also uses this channel for its editable UPS status message unless a
different UPS status channel is configured. Give the bot **View Channel** and
**Send Messages** permissions in that channel. It does not need a new Discord
secret, privileged intent, or **Manage Messages** permission.

The health presence is enabled by default. These optional settings make the
defaults explicit:

```toml
[discord]
health_presence_enabled = true
health_poll_interval_seconds = 30
```

Then open the secret file:

```bash
sudo nano /etc/minecraft-manager/controller.env
```

It should resemble:

```ini
DISCORD_BOT_TOKEN=your-discord-bot-token
MC_WEB_PASSWORD=choose-a-dashboard-password
MC_SESSION_SECRET=generated-random-value
MC_AGENT_TOKEN_HOST1=another-generated-random-value
```

Generate random values with:

```bash
openssl rand -hex 32
```

Each Ubuntu host should have its own agent token.

### Optional: UPS shutdown automation

The controller can watch a USB UPS through NUT. For a CyberPower SX950U attached
to the Pi by USB, configure NUT so this command works on the Pi:

```bash
upsc cyberpower@localhost ups.status
```

During normal power it usually includes `OL`. During a power outage it includes
`OB`, and sometimes `LB` when the battery is low.

Once NUT works, enable the controller UPS block:

```bash
sudo nano /etc/minecraft-manager/controller.toml
```

```toml
[ups]
enabled = true
ups_name = "cyberpower"
status_command = ["/usr/bin/upsc", "cyberpower@localhost", "ups.status"]
charge_command = ["/usr/bin/upsc", "cyberpower@localhost", "battery.charge"]
discord_status_enabled = true
# Optional; omit this to use discord.announcement_channel_id.
# discord_status_channel_id = 123456789012345678
poll_interval_seconds = 15
on_battery_delay_seconds = 30
stop_timeout_seconds = 180
downstream_shutdown_script = "shutdown_host"
local_shutdown_delay_seconds = 15
local_shutdown_command = ["/usr/bin/systemctl", "poweroff"]
```

When NUT first reports `OB` or `LB`, the controller immediately announces the
power event in Discord. Then it waits `on_battery_delay_seconds`. If power comes
back during that delay, it announces that shutdown was canceled. If the UPS is
still on battery after the delay, the controller will:

1. send `stop` to each configured Minecraft server;
2. after every service on a machine stops cleanly, run its `shutdown_host`
   script once for that machine;
3. announce that the Pi is shutting down;
4. run the local Pi shutdown command.

Discord also gets an `/ups` slash command that shows line/battery status and
current battery charge. `/status` posts every configured Minecraft server's
status in the channel; only Discord administrators can run it. `/players` is
usable by everyone and posts each active player with their current Paper
server in the channel.

### Live Discord health status

After the Pi controller is updated, its Discord health display works
automatically. Existing `controller.toml` files do not need the new lines:
`health_presence_enabled` and `discord_status_enabled` both default to `true`,
and the UPS status message defaults to `discord.announcement_channel_id`.
The editable UPS message appears when `[ups].enabled = true` and a Discord
channel is configured.

The controller keeps one UPS message and edits it instead of posting a new one
on every check. It shows:

- current line-power or battery state;
- battery percentage;
- overall health level;
- a summary of online, busy, offline, unreachable, and unknown servers; and
- when the information last changed.

The bot member's activity changes to **Watching Minecraft servers • All Good**,
**Caution**, or **Attention**. The controller evaluates server health every 30
seconds and also reacts to UPS changes:

- **All Good:** every configured server is online and the UPS reports normal
  line power (`OL`).
- **Caution:** a server is offline or busy, the battery charge is unknown, or
  NUT reports a warning state.
- **Attention:** an agent/server is unreachable or reports an unknown/error
  state, or the UPS is unavailable, on battery, or entering a shutdown state.

The message ID is stored at
`/var/lib/minecraft-manager/ups-status-card.json`. It survives controller
restarts and program updates, so the controller resumes editing the same
message. If that Discord message is deleted, the controller creates one
replacement and stores its new ID.

Install the Polkit rule that lets the controller service user power off the Pi:

```bash
cd ~/minecraft-server-manager
sudo install -m 0644 deploy/polkit/minecraft-manager-controller-poweroff.rules \
  /etc/polkit-1/rules.d/49-minecraft-manager-controller-poweroff.rules
```

## 3. Install an agent on each Ubuntu machine

Run this on every Minecraft machine:

```bash
sudo apt update
sudo apt install -y git
cd ~
git clone https://github.com/Monkey5283/minecraft-server-manager.git

sudo bash ~/minecraft-server-manager/deploy/scripts/bootstrap-minecraft-manager \
  agent https://github.com/Monkey5283/minecraft-server-manager.git main
```

Configure the server:

```bash
sudo nano /etc/minecraft-manager/agent.toml
sudo nano /etc/minecraft-manager/agent.env
```

Important:

- The agent server `id` must match the controller server `id`.
- `MC_AGENT_TOKEN` must equal this host's token from `controller.env`.
- Set `working_directory` to the Minecraft server directory.
- Change the systemd service names in `actions` if your service is not named
  `minecraft@survival.service`. For your PaperMC service, use
  `papermc.service`.
- Only commands listed under `actions` and `scripts` can be executed.

Install the restricted sudo rules after replacing `survival` with the correct
server ID and replacing the service name with your real service:

```bash
cd ~/minecraft-server-manager
sudo nano deploy/sudoers/minecraft-manager
sudo visudo -cf deploy/sudoers/minecraft-manager
sudo install -m 0440 deploy/sudoers/minecraft-manager \
  /etc/sudoers.d/minecraft-manager
```

For a PaperMC service named `papermc.service`, `/etc/sudoers.d/minecraft-manager`
should allow these commands:

```sudoers
mcmanager ALL=(root) NOPASSWD: /usr/bin/systemctl start papermc.service
mcmanager ALL=(root) NOPASSWD: /usr/bin/systemctl stop papermc.service
mcmanager ALL=(root) NOPASSWD: /usr/bin/systemctl restart papermc.service
mcmanager ALL=(root) NOPASSWD: /usr/local/sbin/update-papermc survival
mcmanager ALL=(root) NOPASSWD: /usr/local/sbin/backup-minecraft survival
```

If you want UPS automation to shut down this Ubuntu machine after stopping the
Minecraft service, add this script to `[servers.scripts]` in
`/etc/minecraft-manager/agent.toml`:

```toml
shutdown_host = [["sudo", "-n", "/usr/sbin/shutdown", "-h", "+1", "UPS on battery; Minecraft Manager requested host shutdown"]]
```

Then install the restricted shutdown sudo rule:

```bash
cd ~/minecraft-server-manager
sudo visudo -cf deploy/sudoers/minecraft-manager-host-shutdown
sudo install -m 0440 deploy/sudoers/minecraft-manager-host-shutdown \
  /etc/sudoers.d/minecraft-manager-host-shutdown
```

Start the agent:

```bash
sudo systemctl restart mc-manager-agent
sudo systemctl status mc-manager-agent
```

If UFW is enabled, allow agent connections only from the Pi:

```bash
sudo ufw allow from PI_LAN_IP to any port 8766 proto tcp
```

### Dashboard file manager

The web dashboard can browse a server directory, edit UTF-8 text files, create
files and folders, and upload or intentionally overwrite files. File access is
disabled by default for existing configurations and must be enabled separately
for each `[[servers]]` entry on its agent.

Add this beneath the applicable server entry in
`/etc/minecraft-manager/agent.toml`, before the next `[[servers]]` block:

```toml
[servers.file_manager]
enabled = true
root = "/srv/minecraft/survival"
max_edit_size_bytes = 2097152
max_upload_size_bytes = 33554432
```

Use the exact directory for that server. The agent resolves every requested
path against this root and rejects absolute paths, `..` traversal, and symbolic
links that leave it. The text editor accepts UTF-8 files up to 2 MiB by default;
uploads are limited to 32 MiB. Increase the limits only when necessary.

The `mcmanager` service account also needs filesystem permission. For the
included `minecraft@.service`, which runs as `minecraft`, prepare a root under
`/srv/minecraft` with:

```bash
sudo apt install -y acl
server_root=/srv/minecraft/survival
server_user=minecraft
sudo setfacl -R -m "u:mcmanager:rwX,u:${server_user}:rwX" "$server_root"
sudo find "$server_root" -type d -exec setfacl \
  -m "d:u:mcmanager:rwx,d:u:${server_user}:rwx" {} +
sudo systemctl restart mc-manager-agent
```

The default agent systemd sandbox already permits writes under
`/srv/minecraft`. If a server lives under `/home`, create a narrow override so
only its exact directory is exposed to the agent:

```bash
sudo install -d /etc/systemd/system/mc-manager-agent.service.d
sudo cp ~/minecraft-server-manager/deploy/systemd/mc-manager-agent-home-files.conf.example \
  /etc/systemd/system/mc-manager-agent.service.d/files.conf
sudo nano /etc/systemd/system/mc-manager-agent.service.d/files.conf
sudo systemctl daemon-reload
sudo systemctl restart mc-manager-agent
```

Replace both the path in `files.conf` and `server_user` in the ACL commands with
the real server directory and service user. Do not expose an entire home
directory when the Minecraft files occupy only one subdirectory.

After the agent restarts, refresh the dashboard. Servers with file access
enabled show **Manage files**. Saves include a content version, so the dashboard
refuses to overwrite a file that changed on disk after it was opened. Uploads
require a separate overwrite confirmation when a filename already exists.

### Player join, transfer, and leave messages

The controller can post one Discord message for each player's network session.
It edits that same message when the player moves between Paper servers. After
the player has been absent from every tracked Paper server for the configured
grace period, it edits the message one final time to show that the player left.
A later join starts a new message, and every player is tracked independently.

Tracking reads Minecraft's UDP Query player list through the agent running on
each Paper machine. Set `track_players = true` only on Paper controller entries.
Do not set it on the Velocity entry: Velocity is the proxy, while the Lobby and
Vanilla Paper servers report the player's actual location.

The following setup matches this network:

- System 2 is `192.168.1.35` and runs Velocity plus the Lobby Paper server.
- Lobby listens locally on `127.0.0.1:25566`.
- Vanilla is the Paper server on `192.168.1.16:25567`.

#### 1. Update the program first

Run this on the Pi controller, System 2, and the Vanilla machine:

```bash
sudo update-minecraft-manager
```

This installs the new program and systemd unit for that machine, then restarts
its manager service. It preserves everything under `/etc/minecraft-manager`,
so it does not add the new settings to your existing TOML files. Add them with
the following steps.

#### 2. Enable Query on both Paper servers

On System 2, edit the Lobby properties:

```bash
sudo nano /srv/minecraft/lobby/server.properties
```

Edit the existing properties so they contain exactly one of each line:

```properties
enable-query=true
query.port=25566
```

On the Vanilla machine, edit:

```bash
sudo nano /home/monkeycraftvanilla/Vanilla/server.properties
```

Set:

```properties
enable-query=true
query.port=25567
```

Query uses UDP even though players use TCP on the same numbered backend port.
The agent queries its own Paper server, so do not add a router port-forward and
do not open either Query port in UFW. Keep ports `25566` and `25567` private.

#### 3. Tell each agent where its Paper Query endpoint is

On System 2, open:

```bash
sudo nano /etc/minecraft-manager/agent.toml
```

Under the existing Lobby `[[servers]]` entry, before the next `[[servers]]`
entry, add:

```toml
[servers.player_query]
host = "127.0.0.1"
port = 25566
timeout_seconds = 3
```

Do not add a `[servers.player_query]` block to the Velocity entry.

On the Vanilla machine, open the same agent configuration path and add this
under its existing Vanilla/`survival` `[[servers]]` entry:

```toml
[servers.player_query]
host = "192.168.1.16"
port = 25567
timeout_seconds = 3
```

The `host` and `port` are the Paper endpoint as reached from that same machine;
they are not the agent URL and they are not the public Velocity address.

#### 4. Enable tracking on the Pi controller

Open:

```bash
sudo nano /etc/minecraft-manager/controller.toml
```

Add this top-level block before the first `[[servers]]` entry:

```toml
[player_tracking]
enabled = true
poll_interval_seconds = 5
leave_grace_seconds = 10
```

It uses `[discord].announcement_channel_id` by default. To post player sessions
in a different Discord text channel, add its ID to the block:

```toml
channel_id = 123456789012345678
```

The bot needs **View Channel** and **Send Messages** in that channel. It edits
its own messages, so it does not need the **Manage Messages** permission.

Edit the existing Lobby and Vanilla controller entries; do not create duplicate
entries. They should include `track_players = true`:

```toml
[[servers]]
id = "lobby"
name = "Lobby"
agent_url = "http://192.168.1.35:8766"
token_env = "MC_AGENT_TOKEN_HOST2"
track_players = true

[[servers]]
id = "survival"
name = "Vanilla"
agent_url = "http://192.168.1.16:8766"
token_env = "MC_AGENT_TOKEN_HOST1"
track_players = true
```

Leave the existing Velocity entry untracked. It must not contain
`track_players = true`.

#### 5. Restart Paper, the agents, and the controller

On System 2:

```bash
sudo systemctl restart minecraft@lobby.service
sudo systemctl restart mc-manager-agent
sudo systemctl status minecraft@lobby.service mc-manager-agent --no-pager
```

On the Vanilla machine:

```bash
sudo systemctl restart papermc.service
sudo systemctl restart mc-manager-agent
sudo systemctl status papermc.service mc-manager-agent --no-pager
```

Finally, on the Pi:

```bash
sudo systemctl restart mc-manager-controller
sudo systemctl status mc-manager-controller --no-pager
```

With the five-second poll and ten-second leave grace, joins and transfers
normally appear within about 5 to 10 seconds. A leave is finalized after
roughly 10 to 20 seconds. If a Query endpoint is temporarily unavailable, the
monitor keeps the current messages instead of incorrectly declaring that players left.

Active session message IDs are saved in
`/var/lib/minecraft-manager/player-sessions.json`. The controller restores them
after a service restart or `sudo update-minecraft-manager`, so an update can
continue editing the same Discord messages rather than starting duplicates.

### Bedrock cross-play and inventory-free server portals

The managed network-plugin setup installs Geyser and Floodgate on Velocity,
ViaVersion on both Paper backends, and the private MonkeyPortals plugin. Admins
mark portal regions with commands; players transfer by walking through them and
receive no selector item.
See [docs/network-plugins.md](docs/network-plugins.md) for the exact install,
sudo, agent, firewall, and verification steps.

### LinuxGSM-managed Minecraft server

For a LinuxGSM server, use the included LinuxGSM examples instead of the
standard systemd actions. The defaults assume the LinuxGSM user and script are
both named `mcserver`.

First verify the script and tmux session:

```bash
ls -l /home/mcserver/mcserver
sudo -u mcserver tmux list-sessions
```

Copy and customize the agent example:

```bash
sudo cp ~/minecraft-server-manager/config/agent.linuxgsm.example.toml \
  /etc/minecraft-manager/agent.toml
sudo nano /etc/minecraft-manager/agent.toml
```

If the LinuxGSM user, script, or tmux session is not `mcserver`, replace every
occurrence in the file. For PaperMC, the script may be named `pmcserver`.

Install the LinuxGSM systemd override. Replace `/home/mcserver` in the copied
file first if the LinuxGSM home is different:

```bash
sudo install -d /etc/systemd/system/mc-manager-agent.service.d
sudo cp ~/minecraft-server-manager/deploy/systemd/mc-manager-agent-linuxgsm.conf \
  /etc/systemd/system/mc-manager-agent.service.d/linuxgsm.conf
sudo nano /etc/systemd/system/mc-manager-agent.service.d/linuxgsm.conf
```

Install the restricted LinuxGSM sudo rules after changing names and paths to
match the server:

```bash
cd ~/minecraft-server-manager
sudo nano deploy/sudoers/minecraft-manager-linuxgsm
sudo visudo -cf deploy/sudoers/minecraft-manager-linuxgsm
sudo install -m 0440 deploy/sudoers/minecraft-manager-linuxgsm \
  /etc/sudoers.d/minecraft-manager-linuxgsm
sudo systemctl daemon-reload
sudo systemctl restart mc-manager-agent
```

The controller now uses LinuxGSM for start, stop, restart, update, and backup.
Status only checks the LinuxGSM tmux session and never invokes `monitor`, so a
status request cannot unexpectedly restart a deliberately stopped server.

## 4. Start the Pi controller

Back on the Pi:

```bash
sudo systemctl restart mc-manager-controller
sudo systemctl status mc-manager-controller
```

Discord should now provide the `/minecraft` command.

If the service fails, view its logs:

```bash
sudo journalctl -u mc-manager-controller -n 100 --no-pager
```

Agent logs use:

```bash
sudo journalctl -u mc-manager-agent -n 100 --no-pager
```

## 5. Open the dashboard through Tailscale

Install Tailscale on the Pi using its official Linux installation instructions,
then connect the Pi:

```bash
sudo tailscale up
sudo tailscale serve --bg localhost:8080
```

Tailscale prints the private HTTPS dashboard address. Only devices in your
tailnet can reach it. Install Tailscale on your phone or computer and open that
address.

Keep the controller bound to `127.0.0.1`. Do not enable Tailscale Funnel and do
not forward port `8080` through your router.

## 6. Update this program

Configuration and secrets remain in `/etc/minecraft-manager` and are not
replaced during updates. Active player-session state and the editable UPS card
ID under `/var/lib/minecraft-manager` are also preserved.

Update any Pi or Ubuntu installation with:

```bash
sudo update-minecraft-manager
```

The updater pulls the latest `main` branch, installs dependencies, and restarts
the correct service. If the new service fails to stay active, it restores the
previous commit automatically.

On the Pi, the live UPS card and health presence use their enabled-by-default
settings even when the existing TOML does not contain those new options. No new
token, secret, Discord intent, or manual configuration migration is required.

## 7. Configure Minecraft jar updates

The included updater can discover the newest **stable or beta** Paper build
when you choose **Apply update** in Discord. **Alpha builds are never
installed.** It only updates the Minecraft version you explicitly configure;
it will not jump from one Minecraft version to another.

First, update the program on the Pi controller and each Ubuntu agent:

```bash
sudo update-minecraft-manager
```

### Lobby on System 2

On `192.168.1.35`, replace `/etc/minecraft/lobby-update.env` with:

```ini
UPDATE_PROVIDER=paper
PAPER_VERSION=26.2
SERVICE_NAME=minecraft@lobby.service
SERVER_DIR=/srv/minecraft/lobby
JAR_NAME=server.jar
```

Secure and check the file:

```bash
sudo chown root:root /etc/minecraft/lobby-update.env
sudo chmod 600 /etc/minecraft/lobby-update.env
sudo /usr/local/sbin/update-minecraft-jar lobby
```

The last command is a safe direct test. It installs the newest stable or beta
build for `26.2`; it skips the restart when that exact build is already
installed. It never selects an alpha build.

### Vanilla Paper server

On `192.168.1.16`, first confirm the directory and jar filename:

```bash
sudo systemctl show papermc.service \
  -p WorkingDirectory -p ExecStart --no-pager
```

If it shows `/srv/minecraft/survival` and `-jar server.jar`, use
`/etc/minecraft/survival-update.env`:

```ini
UPDATE_PROVIDER=paper
PAPER_VERSION=26.2
SERVICE_NAME=papermc.service
SERVER_DIR=/srv/minecraft/survival
JAR_NAME=server.jar
```

Then secure and test it:

```bash
sudo chown root:root /etc/minecraft/survival-update.env
sudo chmod 600 /etc/minecraft/survival-update.env
sudo /usr/local/sbin/update-minecraft-jar survival
```

If `ExecStart` names a different jar, put that exact filename in `JAR_NAME`.
The server ID in the env filename and command must match the ID in the agent's
`agent.toml`.

After the direct test, use Discord's **Apply update** action normally. Its reply
shows whether it installed a particular Paper stable or beta build or skipped
the restart because that build was already installed.

The updater uses PaperMC's official downloads service with an identifying
User-Agent, verifies the published SHA-256 checksum and jar structure before it
stops Minecraft, saves the old jar as `server.jar.pre-update`, and rolls back if
the service does not remain active. Jar rollback does not undo world-format
changes. Paper recommends making a backup and being present for an update in
case a plugin is incompatible. Make a full backup first whenever changing
`PAPER_VERSION`, and especially before installing a beta.

Official references: [PaperMC downloads service](https://docs.papermc.io/misc/downloads-service/)
and [updating Paper](https://docs.papermc.io/paper/updating/).

### Fixed URL mode

For a non-Paper server or a deliberately pinned jar, static mode remains
available:

```ini
UPDATE_PROVIDER=static
DOWNLOAD_URL=https://trusted-provider.example/server.jar
SHA256=expected-64-character-sha256
SERVICE_NAME=minecraft@SERVER_ID.service
SERVER_DIR=/srv/minecraft/SERVER_ID
JAR_NAME=server.jar
```

Fabric, Forge, and other server types still need a trusted fixed URL or their
own provider-specific resolver.

## Useful files

| File | Purpose |
| --- | --- |
| `/etc/minecraft-manager/controller.toml` | Pi server list and Discord permissions |
| `/etc/minecraft-manager/controller.env` | Pi secrets |
| `/etc/minecraft-manager/agent.toml` | Allowed actions on an Ubuntu host |
| `/etc/minecraft-manager/agent.env` | Ubuntu agent token |
| `/etc/minecraft-manager/update.env` | Program update source |
| `/var/lib/minecraft-manager/ups-status-card.json` | Persisted editable UPS message ID |

Never commit files from `/etc/minecraft-manager` or paste their contents into
Discord.

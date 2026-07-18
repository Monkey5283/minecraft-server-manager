# Minecraft Server Manager

Minecraft Server Manager is a self-hosted control panel for Minecraft servers.
It provides a private web dashboard and Discord commands for checking status,
starting, stopping, and restarting servers.

The manager has two parts:

- **Controller:** runs the dashboard and Discord bot.
- **Agent:** runs on each Minecraft host and executes only the commands you
  explicitly allow.

The controller and agents are intended for a trusted LAN, VPN, or private
overlay network. Do not expose their ports directly to the internet.

## Features

- Start, stop, restart, update, and check Minecraft servers
- Manage multiple servers across multiple Linux hosts
- Control access through Discord users, roles, or administrators
- Use a password-protected web dashboard
- Optionally browse, edit, upload, and download server files from the dashboard
- Optionally track Paper players and server transfers
- Optionally monitor a UPS and perform an orderly shutdown
- Run only allowlisted agent commands without a shell

## Requirements

- A controller running systemd, Python 3.11 or newer, and a Debian-based Linux
  distribution such as Ubuntu Server or Raspberry Pi OS
- One or more Linux hosts running Minecraft as a systemd service
- Network access from the controller to each agent
- A Discord application and bot token
- A Git client and an account that can read this repository
- Optional: Tailscale for private remote dashboard access

The installer creates a dedicated `mcmanager` service account, a Python virtual
environment, configuration files, and systemd services.

## 1. Create a Discord bot

In the [Discord Developer Portal](https://discord.com/developers/applications):

1. Create an application and add a bot.
2. Copy the bot token and keep it private.
3. Under **Installation**, configure a Guild Install with the `bot` and
   `applications.commands` scopes.
4. Give the bot **View Channels** and **Send Messages**, copy the install link,
   and add it to your Discord server.
5. Enable Discord Developer Mode if you want to copy server, channel, user, or
   role IDs.

No privileged Discord intents are required.

## 2. Install the controller

Run these commands on the machine that will host the dashboard and Discord bot:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/Monkey5283/minecraft-server-manager.git
cd minecraft-server-manager
sudo bash deploy/scripts/bootstrap-minecraft-manager \
  controller https://github.com/Monkey5283/minecraft-server-manager.git main
```

The installer prepares the controller but does not start it until you finish
the configuration.

### Configure the controller

Open the controller configuration:

```bash
sudo nano /etc/minecraft-manager/controller.toml
```

For a simple one-server installation, replace its contents with the following
and change the values marked in comments:

```toml
[controller]
bind = "127.0.0.1"
port = 8080

[auth]
web_username = "admin"
web_password_env = "MC_WEB_PASSWORD"
session_secret_env = "MC_SESSION_SECRET"
cookie_secure = false

[discord]
discord_token_env = "DISCORD_BOT_TOKEN"
# Replace 0 with your Discord server ID for faster command registration.
guild_id = 0
# Replace 0 with a channel ID to enable startup announcements.
announcement_channel_id = 0
health_presence_enabled = true
# Discord administrators are always allowed. Add user or role IDs as needed.
allowed_user_ids = []
allowed_role_ids = []

[[servers]]
id = "survival"
name = "Survival"
# Use the agent host's private hostname or private address.
agent_url = "http://minecraft-host.local:8766"
token_env = "MC_AGENT_TOKEN_SURVIVAL_HOST"
```

Server IDs must use lowercase letters, numbers, hyphens, or underscores. The
same `id` must be used in the controller and agent configuration.

### Customize the public `/instructions` command

Any member of the Discord server can use `/instructions`; it does not use the
manager user or role allowlists. Add an `[instructions]` table to
`/etc/minecraft-manager/controller.toml` and replace the uppercase placeholders
in this template:

```toml
[instructions]
message = """
***HOW TO JOIN YOUR_SERVER_NAME***

**Java Edition - Windows, macOS, or Linux**
Go to **Multiplayer -> Add Server**.
**Server Address:** `YOUR_JAVA_HOSTNAME:YOUR_JAVA_PORT`
Click **Done**, select the server, and click **Join Server**.

**Bedrock Edition - Windows, iPhone/iPad, or Android**
Go to **Play -> Servers -> Add Server**.
**Server Name:** `YOUR_SERVER_NAME`
**Server Address:** `YOUR_BEDROCK_HOSTNAME`
**Port:** `YOUR_BEDROCK_PORT`
The server name cannot be empty. Click **Add and Play**.

**Console Bedrock Edition - Xbox, PlayStation, Nintendo Switch, and Switch 2**
No mobile app, DNS changes, ads, or server address entry are required.

**First-time setup:**
1. Sign in to Minecraft with your Microsoft account.
2. Go to **Play -> Friends -> Add Friend/Find Cross-Platform Friends**.
3. Search for and add **`YOUR_CONSOLE_JOIN_GAMERTAG`**.
4. Wait up to one minute for it to add you back.
5. Return to **Play -> Friends -> Joinable Friends**.
6. Select the server session and join.

After setup, open **Play -> Friends** and select the server whenever you want to play.

If it does not appear, fully close and reopen Minecraft. Make sure cross-network multiplayer is allowed and you have the required console subscription.
"""
```

The message supports Discord Markdown and must contain between 1 and 2,000
characters. Discord mentions are suppressed so custom instructions cannot ping
users or roles. If you do not run the console friend broadcaster, replace or
remove the console section. Restart `mc-manager-controller` after changing the
message so the command uses the new text.

Next, open the controller secrets file:

```bash
sudo nano /etc/minecraft-manager/controller.env
```

Set the following values:

```ini
DISCORD_BOT_TOKEN=replace-with-your-bot-token
MC_WEB_PASSWORD=replace-with-a-strong-dashboard-password
MC_SESSION_SECRET=replace-with-a-random-secret
MC_AGENT_TOKEN_SURVIVAL_HOST=replace-with-a-different-random-token
```

Generate secure random values with:

```bash
openssl rand -hex 32
```

Use a different agent token for every Minecraft host. The token is shared only
between that agent and the controller.

## 3. Install an agent

Run these commands on each Linux machine that hosts a Minecraft server:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/Monkey5283/minecraft-server-manager.git
cd minecraft-server-manager
sudo bash deploy/scripts/bootstrap-minecraft-manager \
  agent https://github.com/Monkey5283/minecraft-server-manager.git main
```

### Configure the agent

Open the agent configuration:

```bash
sudo nano /etc/minecraft-manager/agent.toml
```

This minimal example assumes the Minecraft server is managed by
`minecraft@survival.service` and stored in `/srv/minecraft/survival`:

```toml
[agent]
name = "minecraft-host"
bind = "0.0.0.0"
port = 8766
token_env = "MC_AGENT_TOKEN"

[[servers]]
id = "survival"
name = "Survival"
working_directory = "/srv/minecraft/survival"
timeout_seconds = 120

[servers.actions]
start = [["sudo", "-n", "/usr/bin/systemctl", "start", "minecraft@survival.service"]]
stop = [["sudo", "-n", "/usr/bin/systemctl", "stop", "minecraft@survival.service"]]
restart = [["sudo", "-n", "/usr/bin/systemctl", "restart", "minecraft@survival.service"]]
status = [["/usr/bin/systemctl", "is-active", "--quiet", "minecraft@survival.service"]]
```

Change the working directory and every service name to match your Minecraft
installation. Each command is an argument array; the agent never passes these
values through a shell.

Open the agent secret file:

```bash
sudo nano /etc/minecraft-manager/agent.env
```

Set `MC_AGENT_TOKEN` to the same random value used for this host in the
controller secrets file:

```ini
MC_AGENT_TOKEN=replace-with-the-matching-agent-token
```

### Allow the service commands

The `mcmanager` account needs permission to run only the lifecycle commands
listed in `agent.toml`. Open a dedicated sudoers file:

```bash
sudo visudo -f /etc/sudoers.d/minecraft-manager
```

For the example service above, add:

```sudoers
mcmanager ALL=(root) NOPASSWD: /usr/bin/systemctl start minecraft@survival.service
mcmanager ALL=(root) NOPASSWD: /usr/bin/systemctl stop minecraft@survival.service
mcmanager ALL=(root) NOPASSWD: /usr/bin/systemctl restart minecraft@survival.service
```

Validate the file before starting the agent:

```bash
sudo chmod 0440 /etc/sudoers.d/minecraft-manager
sudo visudo -cf /etc/sudoers.d/minecraft-manager
```

If UFW is enabled, allow port `8766` only from the controller. Replace the
placeholder with the controller's private address:

```bash
sudo ufw allow from CONTROLLER_PRIVATE_ADDRESS to any port 8766 proto tcp
```

Do not forward port `8766` through your router.

## 4. Start and verify the services

Start the agent on each Minecraft host:

```bash
sudo systemctl restart mc-manager-agent
sudo systemctl status mc-manager-agent --no-pager
```

From the controller, confirm that the agent is reachable. Replace the hostname
with the same value used in `agent_url`:

```bash
curl http://minecraft-host.local:8766/v1/health
```

Then start the controller:

```bash
sudo systemctl restart mc-manager-controller
sudo systemctl status mc-manager-controller --no-pager
```

The Discord bot should come online and register these commands:

- `/minecraft` manages one configured server.
- `/status` reports all configured servers to Discord administrators.
- `/players` lists players when Paper Query is configured.
- `/instructions` publicly shows the configured server join guide.
- `/ups` reports UPS state when UPS monitoring is configured.

## 5. Open the dashboard safely

The default configuration listens only on `127.0.0.1:8080`. Keep it that way
and use one of the private access methods below.

### Tailscale

On the controller, run:

```bash
sudo setup-minecraft-manager-tailscale
```

The helper installs or configures Tailscale, enables private HTTPS with
Tailscale Serve, and prints the dashboard address. Join the same tailnet from
your computer or phone before opening that address.

Do not enable Tailscale Funnel for this dashboard.

### SSH tunnel

From your computer, create a local tunnel to the controller:

```bash
ssh -L 8080:127.0.0.1:8080 USERNAME@CONTROLLER_HOSTNAME
```

Then open `http://127.0.0.1:8080` in your browser.

Do not forward dashboard port `8080` through your router.

## Adding more servers

To add another server on an existing agent:

1. Add another `[[servers]]` block to that host's `agent.toml`.
2. Add a matching `[[servers]]` block to `controller.toml`.
3. Use the same `agent_url` and `token_env` for servers on the same agent.
4. Add exact sudoers rules for the new systemd service.
5. Restart the agent and controller.

To add another host, install a new agent and give it a new random token. Never
reuse an agent token across hosts.

## Optional features

The repository includes examples for more advanced installations:

- [`config/agent.linux.example.toml`](config/agent.linux.example.toml) includes
  Paper Query, file management, updates, backups, and host shutdown.
- [`config/agent.linuxgsm.example.toml`](config/agent.linuxgsm.example.toml)
  shows a LinuxGSM-managed server.
- [`config/paper-update.env.example`](config/paper-update.env.example) configures
  verified Paper jar updates.
- [`deploy/sudoers/`](deploy/sudoers/) contains restricted sudo examples.
- [`deploy/systemd/`](deploy/systemd/) contains service and sandbox examples.

Only enable the features you need, and grant the `mcmanager` account only the
exact commands and directories required by those features.

### Dashboard file downloads

File downloads use the same opt-in file-manager root as browsing, editing, and
uploads. Add this inside each `[[servers]]` block that should expose files:

```toml
[servers.file_manager]
enabled = true
root = "/srv/minecraft/survival"
max_edit_size_bytes = 2097152
max_upload_size_bytes = 33554432
```

Restart that host's agent after changing the configuration. The dashboard's
**Manage files** screen then shows a **Download** button beside every file.
Downloads require an authenticated dashboard session, remain confined to the
configured root, support binary and large files, and stream through the Pi
instead of being loaded completely into its memory. Directories are not
automatically archived; create a backup archive first when you need to download
a whole world or directory.

## Updating

Run this command on the controller and every agent:

```bash
sudo update-minecraft-manager
```

The updater preserves files under `/etc/minecraft-manager`, installs the latest
configured branch, restarts the correct service, and restores the previous
revision if the new service does not stay active.

## Troubleshooting

Check service status and recent logs:

```bash
sudo systemctl status mc-manager-controller --no-pager
sudo journalctl -u mc-manager-controller -n 100 --no-pager

sudo systemctl status mc-manager-agent --no-pager
sudo journalctl -u mc-manager-agent -n 100 --no-pager
```

Common setup problems:

- **Agent is unreachable:** verify `agent_url`, DNS or private addressing, and
  the host firewall.
- **Agent returns unauthorized:** confirm the agent token matches on both
  machines.
- **Start or stop fails:** make sure `agent.toml` and the sudoers file use the
  exact systemd service name.
- **Discord commands are missing:** confirm the bot was invited with the
  `applications.commands` scope. Setting `guild_id` usually makes initial
  command registration faster.
- **Discord says access is denied:** use a Discord administrator account or add
  the correct user or role ID to `controller.toml`.

## Important files

| Path | Purpose |
| --- | --- |
| `/etc/minecraft-manager/controller.toml` | Controller, Discord, and server settings |
| `/etc/minecraft-manager/controller.env` | Controller secrets |
| `/etc/minecraft-manager/agent.toml` | Agent servers and allowlisted commands |
| `/etc/minecraft-manager/agent.env` | Agent authentication token |
| `/etc/minecraft-manager/update.env` | Installation role and update source |

Never commit files from `/etc/minecraft-manager`, bot tokens, passwords,
session secrets, public addresses, private hostnames, or details of your live
network.

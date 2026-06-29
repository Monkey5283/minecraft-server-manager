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
  `minecraft@survival.service`.
- Only commands listed under `actions` and `scripts` can be executed.

Install the restricted sudo rules after replacing `survival` with the correct
server ID:

```bash
cd ~/minecraft-server-manager
sudo nano deploy/sudoers/minecraft-manager
sudo visudo -cf deploy/sudoers/minecraft-manager
sudo install -m 0440 deploy/sudoers/minecraft-manager \
  /etc/sudoers.d/minecraft-manager
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
replaced during updates.

Update any Pi or Ubuntu installation with:

```bash
sudo update-minecraft-manager
```

The updater pulls the latest `main` branch, installs dependencies, and restarts
the correct service. If the new service fails to stay active, it restores the
previous commit automatically.

## 7. Configure Minecraft jar updates

The example agent action uses the included safe jar updater. On an Ubuntu host,
create `/etc/minecraft/SERVER_ID-update.env`, for example:

```ini
DOWNLOAD_URL=https://trusted-provider.example/server.jar
SHA256=expected-64-character-sha256
```

The updater verifies the checksum before stopping Minecraft. If the updated
server fails to start, it restores the previous jar.

Different server types discover updates differently. Paper, Fabric, Forge, and
Vanilla may require provider-specific download scripts.

## Useful files

| File | Purpose |
| --- | --- |
| `/etc/minecraft-manager/controller.toml` | Pi server list and Discord permissions |
| `/etc/minecraft-manager/controller.env` | Pi secrets |
| `/etc/minecraft-manager/agent.toml` | Allowed actions on an Ubuntu host |
| `/etc/minecraft-manager/agent.env` | Ubuntu agent token |
| `/etc/minecraft-manager/update.env` | Program update source |

Never commit files from `/etc/minecraft-manager` or paste their contents into
Discord.

# LAN Minecraft Manager

A Raspberry Pi–friendly control plane for Minecraft servers running on other
machines in your local network.

It provides:

- `/minecraft` Discord slash commands for status, start, stop, restart, update,
  and allowlisted maintenance scripts
- a mobile-friendly web dashboard
- a small authenticated agent on each Minecraft host
- Ubuntu Server/systemd deployment
- per-server job locking, timeouts, logs, and update rollback support
- configuration-preserving program updates across the whole fleet

## How it is arranged

```text
Discord ─┐
         ├── Raspberry Pi controller ── authenticated LAN HTTP ── host agent ── systemd/service/scripts
Browser ─┘                                                    └─ host agent ── systemd/service/scripts
```

Use **Raspberry Pi OS Lite 64-bit** on the Pi. A custom OS is unnecessary: the
dashboard is served as a web page, so the Pi can remain headless and reliable.
Give the Pi and Minecraft hosts DHCP reservations or static addresses.

The agent is intentionally not a remote shell. Only commands written in its
local TOML file can run. Discord users must also be a server administrator, an
allowed user, or have an allowed role.

## 1. Create the Discord bot

In the Discord Developer Portal:

1. Create an application and bot, then copy its token.
2. Invite it to the server with the `bot` and `applications.commands` scopes.
3. Give it permission to send messages and use application commands.
4. Enable Discord Developer Mode and copy the guild, user, and/or role IDs for
   the controller configuration.

The bot does not need Message Content intent because it uses slash commands.
Do not give it Discord Administrator unless your own server policy requires it.

## 2. Put the project in a write-protected Git repository

The built-in updater pulls reviewed releases from Git. Put this project in a
GitHub/GitLab repository and protect its `main` branch. A public repository is
the easiest option because this project contains no secrets; local secrets are
gitignored and live under `/etc`. For a private repository, give the
`mcmanager` Linux account on every machine a read-only SSH deploy key.

The updater treats repository code as trusted because it installs and restarts
services as root. Never point it at a repository or branch that untrusted people
can write to.

## 3. Install the controller on the Pi

Install Raspberry Pi OS Lite and enable SSH. Clone the project once so the
bootstrap script is available:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/Monkey5283/minecraft-server-manager.git ~/minecraft-manager-setup
sudo bash ~/minecraft-manager-setup/deploy/scripts/bootstrap-minecraft-manager \
  controller https://github.com/Monkey5283/minecraft-server-manager.git main
```

The bootstrapper installs dependencies and systemd files, but does not start the
service with placeholder secrets. Edit:

- `/etc/minecraft-manager/controller.toml`
- `/etc/minecraft-manager/controller.env`
- `/etc/minecraft-manager/fleet`

Generate independent secrets with:

```bash
openssl rand -hex 32
```

Use a different agent token for each host, then start the controller:

```bash
sudo systemctl restart mc-manager-controller
sudo journalctl -u mc-manager-controller -f
```

Open `http://PI_ADDRESS:8080` from a device on the LAN.

## 4. Install an agent on every Ubuntu Minecraft host

On each Ubuntu Server machine:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/Monkey5283/minecraft-server-manager.git ~/minecraft-manager-setup
sudo bash ~/minecraft-manager-setup/deploy/scripts/bootstrap-minecraft-manager \
  agent https://github.com/Monkey5283/minecraft-server-manager.git main
sudoedit /etc/minecraft-manager/agent.toml
sudoedit /etc/minecraft-manager/agent.env
sudo systemctl restart mc-manager-agent
sudo journalctl -u mc-manager-agent -f
```

Set `MC_AGENT_TOKEN` to the token used by this host's controller entry. The
server `id` must also match at both ends.

If Minecraft is not already managed by a service, adapt
`deploy/systemd/minecraft@.service`. Service management is much more reliable
than launching Java directly from the agent because processes survive agent
restarts and shut down cleanly. Its `/etc/minecraft/survival.env` should define,
for example, `JAVA_MIN_MEMORY=2G` and `JAVA_MAX_MEMORY=6G`.

## 5. Update this program without reconfiguring it

Program code, Python dependencies, and systemd definitions can be updated while
all local configuration remains untouched in `/etc/minecraft-manager`.

To update one machine:

```bash
sudo update-minecraft-manager
```

If the new service does not stay active, the updater automatically reinstalls
the previous Git commit and restarts it.

To update every Ubuntu agent plus the controller with one command from the Pi:

1. Create a dedicated SSH account such as `mcdeploy` on each Ubuntu host.
2. Give the Pi user's SSH key access to that account.
3. List targets in `/etc/minecraft-manager/fleet`, one per line.
4. Customize `deploy/sudoers/minecraft-manager-updater`, validate it with
   `visudo -cf`, then install it on each Ubuntu host.
5. Run:

```bash
update-minecraft-manager-fleet
```

Agents are updated first and the Pi controller last. A failed machine is
reported without erasing its existing configuration. The fleet helper requires
non-interactive SSH keys and the exact narrow sudo rule from the example.

The bootstrap command is also safe to rerun: it creates configuration files
only when missing and never replaces existing ones.

## 6. Configure Minecraft updates and scripts

The Ubuntu agent bootstrapper installs `update-minecraft-jar` and
`backup-minecraft`. These are separate from updating this manager program.

Create `/etc/minecraft/survival-update.env`:

```bash
DOWNLOAD_URL=https://your-trusted-provider.example/path/server.jar
SHA256=the_expected_64_character_sha256
```

The updater downloads and verifies the new jar before stopping the server. If
the new service fails to start, it restores the previous jar. Change the URL and
hash when approving a release, then choose **Apply update** in Discord or the
dashboard.

Tailor `deploy/sudoers/minecraft-manager` for every server ID. Always validate
it:

```bash
sudo visudo -cf deploy/sudoers/minecraft-manager
sudo install -m 0440 deploy/sudoers/minecraft-manager /etc/sudoers.d/minecraft-manager
```

Never grant the agent broad passwordless `sudo`, and never configure
`["bash", "-c", ...]`, PowerShell `-Command`, or user-supplied command text.

## Network hardening

- Firewall TCP `8766` on every host so only the Pi's IP can reach it.
- Firewall TCP `8080` on the Pi so only your LAN or management VLAN can reach it.
- Do not port-forward either port to the internet. Use a VPN such as WireGuard or
  Tailscale for remote access.
- Plain HTTP is acceptable only on a trusted, isolated LAN. For a less trusted
  network, put the controller and agents behind HTTPS or a VPN.
- Rotate a token immediately if it appears in logs, screenshots, or source
  control.

## Local development

Python 3.11 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp config/controller.example.toml config/controller.toml
cp config/agent.linux.example.toml config/agent.toml
pytest
```

Run an agent and controller in separate terminals:

```bash
mc-manager-agent --config config/agent.toml
mc-manager-controller --config config/controller.toml
```

## Current limitations

- Jobs are kept in memory; completed job history disappears after an agent
  restart.
- The included updater applies a reviewed URL and checksum. Automatically
  discovering releases is provider-specific and should be added only for the
  server type you actually use (Paper, Fabric, Forge, Vanilla, and so on).
- The sample backup script creates filesystem archives. For production worlds,
  add an RCON-based save flush or stop the server briefly for fully consistent
  backups.

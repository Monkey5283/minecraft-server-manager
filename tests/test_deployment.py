from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_systemd_units_use_stable_install_paths():
    for role in ("controller", "agent"):
        unit = (ROOT / "deploy/systemd" / f"mc-manager-{role}.service").read_text()
        assert "WorkingDirectory=/opt/minecraft-manager/source" in unit
        assert f"ExecStart=/opt/minecraft-manager/venv/bin/mc-manager-{role}" in unit
        assert f"/etc/minecraft-manager/{role}.toml" in unit


def test_updater_never_targets_configuration_directory():
    updater = (ROOT / "deploy/scripts/update-minecraft-manager").read_text()
    assert 'SOURCE_DIR must be below /opt/minecraft-manager' in updater
    assert 'VENV_DIR must be below /opt/minecraft-manager' in updater
    assert "reset --hard" in updater
    assert "reset --hard /etc" not in updater


def test_controller_unit_has_persistent_player_session_state():
    unit = (ROOT / "deploy/systemd/mc-manager-controller.service").read_text()

    assert "StateDirectory=minecraft-manager" in unit
    assert "ProtectSystem=strict" in unit


def test_linuxgsm_override_exposes_home_and_host_tmp():
    override = (
        ROOT / "deploy/systemd/mc-manager-agent-linuxgsm.conf"
    ).read_text()

    assert "ProtectHome=read-only" in override
    assert "ReadWritePaths=/home/mcserver" in override
    assert "PrivateTmp=false" in override


def test_agent_installers_include_network_plugin_updater():
    for installer_name in ("bootstrap-minecraft-manager", "update-minecraft-manager"):
        installer = (ROOT / "deploy/scripts" / installer_name).read_text()
        assert "update-minecraft-plugins" in installer


def test_controller_installers_include_tailscale_setup():
    for installer_name in ("bootstrap-minecraft-manager", "update-minecraft-manager"):
        installer = (ROOT / "deploy/scripts" / installer_name).read_text()
        assert "setup-minecraft-manager-tailscale" in installer


def test_tailscale_setup_keeps_dashboard_private_and_persistent():
    setup = (
        ROOT / "deploy/scripts/setup-minecraft-manager-tailscale"
    ).read_text()

    assert "controller.bind must be 127.0.0.1" in setup
    assert 'tailscale serve --bg --yes "http://127.0.0.1:${dashboard_port}"' in setup
    assert "tailscale serve status" in setup
    assert "\ntailscale funnel" not in setup.lower()
    assert "cookie_secure" in setup


def test_dashboard_assets_include_opt_in_file_manager_controls():
    static = ROOT / "mc_manager" / "static"
    html = (static / "index.html").read_text()
    javascript = (static / "app.js").read_text()

    assert 'id="file-manager"' in html
    assert 'id="editor-content"' in html
    assert "server.files_enabled" in javascript
    assert "expected_version" in javascript
    assert "application/octet-stream" in javascript
    assert "/files/download?path=" in javascript
    assert 'downloadButton.textContent = "Download"' in javascript


def test_dashboard_assets_include_lan_onboarding_and_provisioning():
    static = ROOT / "mc_manager" / "static"
    html = (static / "index.html").read_text()
    javascript = (static / "app.js").read_text()

    assert 'id="open-setup"' in html
    assert 'id="discovered-agents"' in html
    for server_type in ("paper", "vanilla", "forge", "neoforge"):
        assert f'value="{server_type}"' in html
    assert "/api/agents/discovered" in javascript
    assert "/api/agents/pair" in javascript
    assert "/api/agents/configured" in javascript
    assert "/api/agents/adopt" in javascript
    assert "Already configured agents" in html
    assert "/catalog/" in javascript


def test_dashboard_assets_include_backup_first_per_server_software_change():
    static = ROOT / "mc_manager" / "static"
    html = (static / "index.html").read_text()
    javascript = (static / "app.js").read_text()
    sudoers = (ROOT / "deploy/sudoers/minecraft-manager-provisioning").read_text()
    project = (ROOT / "pyproject.toml").read_text()

    assert 'id="software-panel"' in html
    assert 'id="software-type"' in html
    assert "Change software/version" in javascript
    assert "/software" in javascript
    assert "confirm_backup" in javascript
    assert "mc-manager-change-software --request *" in sudoers
    assert "mc-manager-change-software" in project


def test_dashboard_assets_include_guarded_managed_server_deletion():
    static = ROOT / "mc_manager" / "static"
    html = (static / "index.html").read_text()
    javascript = (static / "app.js").read_text()
    sudoers = (ROOT / "deploy/sudoers/minecraft-manager-provisioning").read_text()
    project = (ROOT / "pyproject.toml").read_text()

    assert 'id="delete-panel"' in html
    assert "Delete server" in javascript
    assert "server.server_delete_enabled" in javascript
    assert "/api/servers/${server.controller_id}/delete" in javascript
    assert "delete_backups" in javascript
    assert "mc-manager-delete-server --request *" in sudoers
    assert "mc-manager-delete-server" in project


def test_dashboard_assets_include_scoped_files_and_minecraft_console():
    static = ROOT / "mc_manager" / "static"
    html = (static / "index.html").read_text()
    javascript = (static / "app.js").read_text()
    launcher = (ROOT / "deploy/scripts/start-minecraft-server").read_text()
    installer = (ROOT / "mc_manager/server_installer.py").read_text()

    assert 'id="save-and-restart"' in html
    assert 'id="console-panel"' in html
    assert "Minecraft server commands only" in html
    assert 'method: "DELETE"' in javascript
    assert "server.console_enabled" in javascript
    assert 'exec 3<>"$console_pipe"' in launcher
    assert "mkfifo -m 0660" in launcher
    assert '"input_pipe": f"/srv/minecraft/{server_id}/.manager/console.in"' in installer


def test_agent_installer_enables_safe_dashboard_provisioning():
    bootstrap = (ROOT / "deploy/scripts/bootstrap-minecraft-manager").read_text()
    unit = (ROOT / "deploy/systemd/mc-manager-agent.service").read_text()
    sudoers = (ROOT / "deploy/sudoers/minecraft-manager-provisioning").read_text()

    assert "agent.onboarding.example.toml" in bootstrap
    assert "secrets.token_hex(32)" in bootstrap
    assert "default-jre-headless" in bootstrap
    assert "minecraft@.service" in bootstrap
    assert "start-minecraft-server" in bootstrap
    assert "ReadWritePaths=-/etc/systemd/system" in unit
    assert "mc-manager-provision --request *" in sudoers
    assert "mc-manager-managed-action *" in sudoers
    assert "mc-manager-delete-server --request *" in sudoers
    assert "install -d --owner=root --group=minecraft --mode=2770 /srv/minecraft-backups" in bootstrap


def test_agent_updater_precreates_writable_backup_directory():
    updater = (ROOT / "deploy/scripts/update-minecraft-manager").read_text()
    assert "install -d --owner=root --group=minecraft --mode=2770 /srv/minecraft-backups" in updater


def test_home_file_manager_override_is_narrow_and_opt_in():
    override = (
        ROOT / "deploy/systemd/mc-manager-agent-home-files.conf.example"
    ).read_text()

    assert "ProtectHome=read-only" in override
    assert "ReadWritePaths=/home/minecraft-user/server-directory" in override
    assert "ReadWritePaths=/home\n" not in override


def test_plugin_updater_uses_profiles_checksums_and_transactional_rollback():
    updater = (ROOT / "deploy/scripts/update-minecraft-plugins").read_text()

    assert "velocity-crossplay|lobby-network|paper-network" in updater
    assert 'add_geyser_download "Geyser-Velocity.jar" "geyser"' in updater
    assert 'add_geyser_download "floodgate-velocity.jar" "floodgate"' in updater
    assert '"MCXboxBroadcast/Broadcaster"' in updater
    assert '"Geyser-Velocity/extensions/MCXboxBroadcastExtension.jar"' in updater
    assert "resolve_github_release_asset" in updater
    assert 'digest.startswith("sha256:")' in updater
    assert "MCXBOX_BROADCAST_ADDRESS is required" in updater
    assert "MCXBOX_BROADCAST_PORT is required" in updater
    assert 'broadcaster_data_dir="${plugins_dir}/Geyser-Velocity/extensions/mcxboxbroadcast"' in updater
    assert "if [[ ! -e \"$broadcaster_config\" ]]" in updater
    assert 'add_local_plugin "MonkeyPortals.jar"' in updater
    assert 'add_modrinth_download "ViaVersion.jar"' in updater
    assert 'add_modrinth_download "ViaBackwards.jar"' in updater
    assert 'add_local_plugin "MonkeyLobbyMusic.jar"' not in updater
    assert '"MonkeyLobbyMusic.jar"' in updater
    assert '"MonkeyLobbyMusic-*.jar"' in updater
    assert '/run/sudo/minecraft-plugin-update-${server_id}.lock' in updater
    assert "sha512sum --check" in updater
    assert "sha256sum --check" in updater
    assert "jar --list --file" in updater
    assert "rollback_plugins" in updater
    assert '"ServerSelector.jar"' in updater
    assert "must be owned by root" in updater
    assert "must not be writable by group or other" in updater


def test_lobby_music_playlist_is_embedded_in_monkey_portals():
    for installer_name in ("bootstrap-minecraft-manager", "update-minecraft-manager"):
        installer = (ROOT / "deploy/scripts" / installer_name).read_text()
        assert "minecraft-plugins/monkey-lobby-music/dist/MonkeyLobbyMusic.jar" not in installer
        assert "/usr/local/share/minecraft-manager/MonkeyLobbyMusic.jar" not in installer

    plugin_config = (
        ROOT / "minecraft-plugins/monkey-portals/src/main/resources/config.yml"
    ).read_text()
    plugin_yml = (
        ROOT / "minecraft-plugins/monkey-portals/src/main/resources/plugin.yml"
    ).read_text()
    assert "monkeycraft_nexus_awaits.nbs" in plugin_config
    assert "monkeycraft_festival_of_the_skyways.nbs" in plugin_config
    assert "gap-seconds: 5" in plugin_config
    assert "lobbymusic:" in plugin_yml
    assert "aliases: [lmusic, radio]" in plugin_yml
    assert "/run/sudo/minecraft-manager-update.lock" in (
        ROOT / "deploy/scripts/update-minecraft-manager"
    ).read_text()


def test_jar_updater_supports_safe_on_demand_paper_updates():
    updater = (ROOT / "deploy/scripts/update-minecraft-jar").read_text()

    assert 'UPDATE_PROVIDER:-static' in updater
    assert "mc-manager-resolve-paper" in updater
    assert 'PAPER_VERSION:?PAPER_VERSION is required' in updater
    assert 'paper_channel" != "STABLE"' in updater
    assert 'paper_channel" != "BETA"' in updater
    assert "sha256sum --check" in updater
    assert "jar --list --file" in updater
    assert "already running" in updater
    assert "handle_update_failure" in updater
    assert "previous server jar restored" in updater
    assert updater.index('current_sha256="') < updater.index('systemctl stop "$service"')


def test_monkey_portals_is_inventory_free_and_targets_velocity_ids():
    resources = ROOT / "minecraft-plugins/monkey-portals/src/main/resources"
    config = (resources / "config.yml").read_text()
    plugin = (resources / "plugin.yml").read_text()

    assert "- lobby" in config
    assert "- vanilla" in config
    assert "mportal:" in plugin
    assert "monkeyportals.admin" in plugin
    assert "selector" not in config.lower()


def test_monkey_portals_supports_an_optional_safe_arrival_spawn():
    source = ROOT / "minecraft-plugins/monkey-portals/src/main/java/dev/monkeycraft/portals"
    command = (source / "PortalCommand.java").read_text()
    listener = (source / "PortalListener.java").read_text()
    config = (
        ROOT / "minecraft-plugins/monkey-portals/src/main/resources/config.yml"
    ).read_text()

    assert 'case "setspawn"' in command
    assert 'case "clearspawn"' in command
    assert "PlayerJoinEvent" in listener
    assert "player.teleport(arrival)" in listener
    assert "arrival-spawn:" in config
    assert "enabled: false" in config


def test_monkey_portals_supports_typed_local_destinations_and_legacy_files():
    source = ROOT / "minecraft-plugins/monkey-portals/src/main/java/dev/monkeycraft/portals"
    command = (source / "PortalCommand.java").read_text()
    listener = (source / "PortalListener.java").read_text()
    repository = (source / "PortalRepository.java").read_text()
    target = (source / "PortalTarget.java").read_text()

    assert "PortalTarget.Location" in command
    assert "PortalTarget.World" in command
    assert "PortalTarget.Portal" in command
    assert 'case "setlocation"' in command
    assert 'case "setworld"' in command
    assert 'case "setportal"' in command
    assert "player.teleport(" in listener
    assert "destinationPortal" in listener
    assert 'case "location"' in repository
    assert 'case "world"' in repository
    assert 'case "portal"' in repository
    assert 'path + ".server"' in repository
    assert "sealed interface PortalTarget" in target

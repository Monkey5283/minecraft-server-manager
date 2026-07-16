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


def test_dashboard_assets_include_opt_in_file_manager_controls():
    static = ROOT / "mc_manager" / "static"
    html = (static / "index.html").read_text()
    javascript = (static / "app.js").read_text()

    assert 'id="file-manager"' in html
    assert 'id="editor-content"' in html
    assert "server.files_enabled" in javascript
    assert "expected_version" in javascript
    assert "application/octet-stream" in javascript


def test_dashboard_assets_include_managed_server_setup_controls():
    static = ROOT / "mc_manager" / "static"
    html = (static / "index.html").read_text()
    javascript = (static / "app.js").read_text()

    assert 'id="manage-servers"' in html
    assert 'id="server-registry"' in html
    assert "/api/server-registry/discover" in javascript
    assert "source_server_id" in javascript
    assert "confirm_id" in javascript


def test_home_file_manager_override_is_narrow_and_opt_in():
    override = (
        ROOT / "deploy/systemd/mc-manager-agent-home-files.conf.example"
    ).read_text()

    assert "ProtectHome=read-only" in override
    assert "ReadWritePaths=/home/minecraft-user/server-directory" in override
    assert "ReadWritePaths=/home\n" not in override


def test_agent_installers_include_network_plugin_updater_and_template():
    for installer_name in ("bootstrap-minecraft-manager", "update-minecraft-manager"):
        installer = (ROOT / "deploy/scripts" / installer_name).read_text()
        assert "update-minecraft-plugins" in installer
        assert "plugin-config/server-selector.yml" in installer


def test_plugin_updater_uses_profiles_checksums_and_transactional_rollback():
    updater = (ROOT / "deploy/scripts/update-minecraft-plugins").read_text()

    assert "velocity-crossplay|lobby-network|paper-network" in updater
    assert 'add_geyser_download "Geyser-Velocity.jar" "geyser"' in updater
    assert 'add_geyser_download "floodgate-velocity.jar" "floodgate"' in updater
    assert 'add_modrinth_download "ServerSelector.jar"' in updater
    assert 'add_modrinth_download "ViaVersion.jar"' in updater
    assert 'add_modrinth_download "ViaBackwards.jar"' in updater
    assert "sha512sum --check" in updater
    assert "sha256sum --check" in updater
    assert "jar --list --file" in updater
    assert "rollback_plugins" in updater
    assert "Preserving existing ServerSelector configuration" in updater


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


def test_server_selector_template_targets_the_velocity_server_ids():
    config = (ROOT / "deploy/plugin-config/server-selector.yml").read_text()

    assert 'command: "server:lobby"' in config
    assert 'command: "server:vanilla"' in config
    assert "give-on-every-join: true" in config
    assert "lock-selector-item: true" in config

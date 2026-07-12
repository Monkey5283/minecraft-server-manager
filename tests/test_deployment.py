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

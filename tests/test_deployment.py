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

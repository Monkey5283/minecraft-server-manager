from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from mc_manager import server_installer
from mc_manager.server_installer import InstallError, delete_managed_server


def configured_server(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    minecraft_root = tmp_path / "minecraft"
    backup_root = tmp_path / "backups"
    server_dir = minecraft_root / "vanillaplus"
    (server_dir / "world").mkdir(parents=True)
    (server_dir / "world" / "level.dat").write_bytes(b"important world")
    (server_dir / "server.properties").write_text("motd=Vanilla Plus\n")
    registry_path = minecraft_root / ".manager" / "managed-servers.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": [{"id": "vanillaplus", "name": "Vanilla Plus"}],
            }
        )
    )
    monkeypatch.setattr(server_installer, "MINECRAFT_ROOT", minecraft_root)
    monkeypatch.setattr(server_installer, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(server_installer, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(server_installer, "_service_active", lambda *_: True)
    systemctl = []
    monkeypatch.setattr(
        server_installer,
        "_systemctl",
        lambda action, service, check=True: systemctl.append((action, service, check)),
    )
    return server_dir, registry_path, backup_root


def test_delete_requires_exact_confirmation_without_touching_server(
    tmp_path: Path, monkeypatch
) -> None:
    server_dir, registry_path, _backup_root = configured_server(tmp_path, monkeypatch)

    with pytest.raises(InstallError, match="exactly match"):
        delete_managed_server({"id": "vanillaplus", "confirmation": "wrong"})

    assert server_dir.is_dir()
    assert json.loads(registry_path.read_text())["servers"][0]["id"] == "vanillaplus"


def test_delete_stops_backs_up_and_removes_only_selected_server(
    tmp_path: Path, monkeypatch
) -> None:
    server_dir, registry_path, backup_root = configured_server(tmp_path, monkeypatch)
    sibling = server_dir.parent / "creative"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep")

    result = delete_managed_server(
        {"id": "vanillaplus", "confirmation": "vanillaplus"}
    )

    assert result["state"] == "deleted"
    assert not server_dir.exists()
    assert (sibling / "keep.txt").read_text() == "keep"
    assert json.loads(registry_path.read_text())["servers"] == []
    backup = Path(result["backup"])
    assert backup.is_file()
    assert backup.parent == backup_root / "vanillaplus"


def test_delete_backup_failure_restores_service_without_removing_data(
    tmp_path: Path, monkeypatch
) -> None:
    server_dir, registry_path, _backup_root = configured_server(tmp_path, monkeypatch)
    systemctl_calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        server_installer,
        "_systemctl",
        lambda action, service, check=True: systemctl_calls.append(
            (action, service, check)
        ),
    )
    monkeypatch.setattr(
        server_installer,
        "_deletion_backup",
        lambda *_: (_ for _ in ()).throw(InstallError("backup failed")),
    )

    with pytest.raises(InstallError, match="backup failed"):
        delete_managed_server(
            {"id": "vanillaplus", "confirmation": "vanillaplus"}
        )

    assert (server_dir / "world" / "level.dat").read_bytes() == b"important world"
    assert json.loads(registry_path.read_text())["servers"][0]["id"] == "vanillaplus"
    assert ("stop", "minecraft@vanillaplus.service", True) in systemctl_calls
    assert ("start", "minecraft@vanillaplus.service", False) in systemctl_calls


def test_standard_legacy_delete_is_allowlisted_and_writes_tombstone(
    tmp_path: Path, monkeypatch
) -> None:
    server_dir, registry_path, _backup_root = configured_server(tmp_path, monkeypatch)
    registry_path.write_text('{"version":1,"servers":[]}', encoding="utf-8")
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        '''[[servers]]
id = "vanillaplus"
working_directory = "/srv/minecraft/vanillaplus"
[servers.actions]
stop = [["sudo", "-n", "/usr/bin/systemctl", "stop", "minecraft@vanillaplus.service"]]
''',
        encoding="utf-8",
    )
    monkeypatch.setattr(server_installer, "AGENT_CONFIG_PATH", agent_config)

    result = delete_managed_server(
        {
            "id": "vanillaplus",
            "confirmation": "vanillaplus",
            "legacy": True,
        }
    )

    assert result["legacy"] is True
    assert not server_dir.exists()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["deleted_legacy_servers"] == ["vanillaplus"]


def test_legacy_delete_rejects_custom_or_unlisted_layout(
    tmp_path: Path, monkeypatch
) -> None:
    server_dir, registry_path, _backup_root = configured_server(tmp_path, monkeypatch)
    registry_path.write_text('{"version":1,"servers":[]}', encoding="utf-8")
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        '''[[servers]]
id = "vanillaplus"
working_directory = "/custom/vanillaplus"
[servers.actions]
stop = [["sudo", "-n", "/usr/bin/systemctl", "stop", "custom.service"]]
''',
        encoding="utf-8",
    )
    monkeypatch.setattr(server_installer, "AGENT_CONFIG_PATH", agent_config)

    with pytest.raises(InstallError, match="not allowlisted"):
        delete_managed_server(
            {
                "id": "vanillaplus",
                "confirmation": "vanillaplus",
                "legacy": True,
            }
        )

    assert server_dir.exists()


def test_standard_legacy_delete_can_remove_stale_registration_without_files(
    tmp_path: Path, monkeypatch
) -> None:
    server_dir, registry_path, _backup_root = configured_server(tmp_path, monkeypatch)
    shutil.rmtree(server_dir)
    registry_path.write_text('{"version":1,"servers":[]}', encoding="utf-8")
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        '''[[servers]]
id = "vanillaplus"
working_directory = "/srv/minecraft/vanillaplus"
[servers.actions]
stop = [["sudo", "-n", "/usr/bin/systemctl", "stop", "minecraft@vanillaplus.service"]]
''',
        encoding="utf-8",
    )
    monkeypatch.setattr(server_installer, "AGENT_CONFIG_PATH", agent_config)
    backup = Mock()
    monkeypatch.setattr(server_installer, "_deletion_backup", backup)

    result = delete_managed_server(
        {
            "id": "vanillaplus",
            "confirmation": "vanillaplus",
            "legacy": True,
        }
    )

    assert result["legacy"] is True
    assert result["backup"] is None
    backup.assert_not_called()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["deleted_legacy_servers"] == ["vanillaplus"]

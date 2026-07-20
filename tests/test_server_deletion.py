from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mc_manager import server_installer
from mc_manager.server_installer import InstallError, delete_managed_server


def configured_server(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    minecraft_root = tmp_path / "minecraft"
    backup_root = tmp_path / "backups"
    server_dir = minecraft_root / "vanillaplus"
    (server_dir / "world").mkdir(parents=True)
    (server_dir / "world" / "level.dat").write_bytes(b"important world")
    registry_path = minecraft_root / ".manager" / "managed-servers.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": [
                    {
                        "id": "vanillaplus",
                        "name": "Vanilla Plus",
                        "working_directory": str(server_dir),
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(server_installer, "MINECRAFT_ROOT", minecraft_root)
    monkeypatch.setattr(server_installer, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(server_installer, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "chown", lambda *_: None, raising=False)
    return server_dir, backup_root, registry_path


def request(*, delete_backups: bool = False, confirm_id: str = "vanillaplus") -> dict:
    return {
        "id": "vanillaplus",
        "confirm_id": confirm_id,
        "delete_backups": delete_backups,
    }


def test_delete_server_keeps_final_backup_by_default(tmp_path: Path, monkeypatch):
    server_dir, backup_root, registry_path = configured_server(tmp_path, monkeypatch)
    active = iter((True, False))
    monkeypatch.setattr(server_installer, "_service_active", lambda *_: next(active))
    systemctl = []
    monkeypatch.setattr(
        server_installer,
        "_systemctl",
        lambda action, service, check=True: systemctl.append((action, service, check)),
    )

    result = delete_managed_server(request())

    assert not server_dir.exists()
    assert json.loads(registry_path.read_text())["servers"] == []
    final_backup = Path(result["final_backup"])
    assert final_backup.is_file()
    assert final_backup.parent == backup_root / "vanillaplus"
    assert result["backups_deleted"] is False
    assert systemctl == [
        ("stop", "minecraft@vanillaplus.service", False),
        ("disable", "minecraft@vanillaplus.service", False),
    ]


def test_delete_server_can_permanently_remove_backups(tmp_path: Path, monkeypatch):
    server_dir, backup_root, registry_path = configured_server(tmp_path, monkeypatch)
    old_backup = backup_root / "vanillaplus" / "old.tar.gz"
    old_backup.parent.mkdir(parents=True)
    old_backup.write_bytes(b"backup")
    monkeypatch.setattr(server_installer, "_service_active", lambda *_: False)
    monkeypatch.setattr(server_installer, "_systemctl", lambda *_args, **_kwargs: None)

    result = delete_managed_server(request(delete_backups=True))

    assert not server_dir.exists()
    assert not old_backup.parent.exists()
    assert json.loads(registry_path.read_text())["servers"] == []
    assert result["final_backup"] is None
    assert result["backups_deleted"] is True


def test_delete_server_requires_exact_id_confirmation(tmp_path: Path, monkeypatch):
    server_dir, _, registry_path = configured_server(tmp_path, monkeypatch)

    with pytest.raises(InstallError, match="confirmation did not match"):
        delete_managed_server(request(confirm_id="wrong"))

    assert server_dir.is_dir()
    assert len(json.loads(registry_path.read_text())["servers"]) == 1


def test_delete_server_restarts_when_final_backup_fails(tmp_path: Path, monkeypatch):
    server_dir, _, registry_path = configured_server(tmp_path, monkeypatch)
    active = iter((True, False))
    monkeypatch.setattr(server_installer, "_service_active", lambda *_: next(active))
    systemctl = []
    monkeypatch.setattr(
        server_installer,
        "_systemctl",
        lambda action, service, check=True: systemctl.append((action, service, check)),
    )
    monkeypatch.setattr(
        server_installer,
        "_deletion_backup",
        lambda *_: (_ for _ in ()).throw(InstallError("backup failed")),
    )

    with pytest.raises(InstallError, match="backup failed"):
        delete_managed_server(request())

    assert server_dir.is_dir()
    assert len(json.loads(registry_path.read_text())["servers"]) == 1
    assert systemctl == [
        ("stop", "minecraft@vanillaplus.service", False),
        ("start", "minecraft@vanillaplus.service", False),
    ]

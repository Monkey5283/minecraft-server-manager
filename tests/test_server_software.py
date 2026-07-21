from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mc_manager import server_installer
from mc_manager.server_catalog import DownloadSpec
from mc_manager.server_installer import InstallError, change_server_software


def configured_server(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    minecraft_root = tmp_path / "minecraft"
    backup_root = tmp_path / "backups"
    server_dir = minecraft_root / "vanillaplus"
    (server_dir / "world").mkdir(parents=True)
    (server_dir / "world" / "level.dat").write_bytes(b"important world")
    (server_dir / "server.properties").write_text("motd=Vanilla Plus\n")
    (server_dir / "server.jar").write_bytes(b"old jar")
    (server_dir / "start-server").write_text("old launcher\n")
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
                        "actions": {"update": [["old-update"]]},
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
    monkeypatch.setattr(
        server_installer,
        "_resolve",
        lambda *_: DownloadSpec("https://example.test/server.jar", "00", "sha256", 17),
    )
    monkeypatch.setattr(server_installer, "_validate_java", lambda *_: Path("/usr/bin/java"))
    monkeypatch.setattr(server_installer, "_minecraft_identity", lambda: (1000, 1000))
    monkeypatch.setattr(server_installer, "_chown_tree", lambda *_: None)
    monkeypatch.setattr(server_installer.time, "sleep", lambda *_: None)

    def stage(staging, server_type, spec, java, minimum_memory, maximum_memory):
        (staging / "server.jar").write_bytes(b"new paper jar")
        (staging / "start-server").write_text("new launcher\n")

    monkeypatch.setattr(server_installer, "_stage_software", stage)
    return server_dir, registry_path


def change_request() -> dict:
    return {
        "id": "vanillaplus",
        "type": "paper",
        "version": "1.21.11",
        "minimum_memory": "2G",
        "maximum_memory": "6G",
        "java_path": str(Path("/usr/bin/java")),
        "accept_eula": True,
        "confirm_backup": True,
    }


def test_change_software_preserves_world_and_records_backup(tmp_path: Path, monkeypatch):
    server_dir, registry_path = configured_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server_installer, "_service_active", lambda *_: False)
    systemctl = []
    monkeypatch.setattr(
        server_installer,
        "_systemctl",
        lambda action, service, check=True: systemctl.append((action, service)),
    )

    result = change_server_software(change_request())

    assert (server_dir / "world" / "level.dat").read_bytes() == b"important world"
    assert (server_dir / "server.properties").read_text() == "motd=Vanilla Plus\n"
    assert (server_dir / "server.jar").read_bytes() == b"new paper jar"
    registry = json.loads(registry_path.read_text())
    assert registry["servers"][0]["software"] == {
        "type": "paper",
        "version": "1.21.11",
        "java_path": str(Path("/usr/bin/java")),
        "minimum_memory": "2G",
        "maximum_memory": "6G",
    }
    assert "update" in registry["servers"][0]["actions"]
    assert Path(result["backup"]).is_file()
    assert result["restarted"] is False
    assert systemctl == [("stop", "minecraft@vanillaplus.service")]


def test_change_software_rolls_back_when_new_runtime_dies(tmp_path: Path, monkeypatch):
    server_dir, registry_path = configured_server(tmp_path, monkeypatch)
    active = iter((True, False))
    monkeypatch.setattr(server_installer, "_service_active", lambda *_: next(active))
    systemctl = []
    monkeypatch.setattr(
        server_installer,
        "_systemctl",
        lambda action, service, check=True: systemctl.append((action, service)),
    )

    with pytest.raises(InstallError, match="restoring its backup"):
        change_server_software(change_request())

    assert (server_dir / "server.jar").read_bytes() == b"old jar"
    assert (server_dir / "world" / "level.dat").read_bytes() == b"important world"
    registry = json.loads(registry_path.read_text())
    assert "software" not in registry["servers"][0]
    assert systemctl == [
        ("stop", "minecraft@vanillaplus.service"),
        ("start", "minecraft@vanillaplus.service"),
        ("stop", "minecraft@vanillaplus.service"),
        ("start", "minecraft@vanillaplus.service"),
    ]


def test_stage_velocity_downloads_proxy_jar_and_writes_proxy_launcher(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        server_installer,
        "_download",
        lambda _spec, destination: destination.write_bytes(b"velocity jar"),
    )

    server_installer._stage_software(
        tmp_path,
        "velocity",
        DownloadSpec("https://example.test/velocity.jar", "00", "sha256", 21),
        Path("/usr/bin/java"),
        "512M",
        "1G",
    )

    assert (tmp_path / "velocity.jar").read_bytes() == b"velocity jar"
    launcher = (tmp_path / "start-server").read_text()
    assert "-jar velocity.jar" in launcher
    assert "nogui" not in launcher


def test_velocity_proxy_cannot_be_converted_to_backend_software(
    tmp_path: Path, monkeypatch
):
    _server_dir, registry_path = configured_server(tmp_path, monkeypatch)
    registry = json.loads(registry_path.read_text())
    registry["servers"][0]["software"] = {"type": "velocity", "version": "4.0.0"}
    registry_path.write_text(json.dumps(registry))

    with pytest.raises(InstallError, match="Velocity proxies can only"):
        change_server_software(change_request())

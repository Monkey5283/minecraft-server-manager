import json
import os
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from mc_manager.launcher import game, runtime_worker
from mc_manager.launcher.auth import PlayerSession, SignInRequired
from mc_manager.launcher.installer import InstallCancelled, InstallConflict, install_release
from mc_manager.launcher.instance import atomic_json, read_json
from mc_manager.launcher.models import ReleaseManifest


def manifest(loader="vanilla"):
    return ReleaseManifest(profile_id="test", version="1", minecraft_version="1.21.1", java_major=21,
                           loader={"kind": loader, "version": None if loader == "vanilla" else "1.2.3"})


def session():
    return PlayerSession("12345678-1234-1234-1234-123456789abc", "1" * 32, "Player", "SENSITIVE-GAME-TOKEN", time.time() + 3600, "123")


def prepared(root, release=None):
    release = release or manifest()
    install_release(release, root)
    runtime = root / ".launcher/minecraft"
    folder = runtime / "versions/1.21.1"
    folder.mkdir(parents=True)
    (folder / "1.21.1.jar").write_bytes(b"jar")
    atomic_json(folder / "1.21.1.json", {
        "id": "1.21.1", "type": "release", "mainClass": "net.minecraft.client.main.Main", "libraries": [],
        "arguments": {"jvm": ["-cp", "${classpath}"], "game": ["--username", "${auth_player_name}",
            "--accessToken", "${auth_access_token}", "--clientId", "${clientid}", "--xuid", "${auth_xuid}", "--gameDir", "${game_directory}"]}})
    atomic_json(root / ".launcher/game.json", {"schema_version": 1, "runtime_key": game.runtime_key(release),
                                                "version_id": "1.21.1", "java": str(root / "java.exe")})
    return release


def test_launch_command_uses_authenticated_identity_memory_and_isolated_directory(tmp_path, monkeypatch):
    pytest.importorskip("minecraft_launcher_lib")
    release = prepared(tmp_path)
    monkeypatch.setattr(game, "java_major", lambda _: 21)
    command = game.build_command(release, tmp_path, session(), memory_mb=6144)
    assert command[command.index("--accessToken") + 1] == "SENSITIVE-GAME-TOKEN"
    assert command[command.index("--gameDir") + 1] == str(tmp_path)
    assert command[command.index("--clientId") + 1] == session().client_id
    assert command[command.index("--xuid") + 1] == "123"
    assert "-Xmx6144M" in command and "--demo" not in command
    assert not any("${" in part for part in command)
    (tmp_path / ".launcher/minecraft/versions/1.21.1/1.21.1.jar").unlink()
    with pytest.raises(ValueError, match="library is missing"):
        game.build_command(release, tmp_path, session(), memory_mb=4096)


def test_launch_refuses_missing_mismatched_or_interrupted_installs(tmp_path):
    release = manifest()
    with pytest.raises(ValueError, match="Install the selected"):
        game.installed_game(release, tmp_path)
    prepared(tmp_path)
    with pytest.raises(ValueError, match="selected pack"):
        game.installed_game(release.model_copy(update={"version": "2"}), tmp_path)
    path = tmp_path / ".launcher/game.json"
    data = read_json(path)
    atomic_json(path, {**data, "runtime_key": "bad"})
    with pytest.raises(ValueError, match="does not match"):
        game.installed_game(release, tmp_path)
    (tmp_path / ".launcher/transaction.json").write_text("{}")
    with pytest.raises(ValueError, match="Recover"):
        game.installed_game(release, tmp_path)


def test_session_and_java_checks(tmp_path, monkeypatch):
    release = prepared(tmp_path)
    expired = PlayerSession(session().client_id, "1" * 32, "Player", "token", 0)
    with pytest.raises(SignInRequired):
        game.build_command(release, tmp_path, expired, memory_mb=4096)
    with pytest.raises(ValueError, match="memory"):
        game.build_command(release, tmp_path, session(), memory_mb=100000)
    pytest.importorskip("minecraft_launcher_lib")
    monkeypatch.setattr(game, "java_major", lambda _: 17)
    with pytest.raises(ValueError, match="Java 21"):
        game.build_command(release, tmp_path, session(), memory_mb=4096)


@pytest.mark.parametrize("version,expected", [('openjdk version "21.0.3"', 21), ('java version "1.8.0_401"', 8)])
def test_java_probe(tmp_path, monkeypatch, version, expected):
    executable = tmp_path / "java.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(game.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="", stderr=version))
    assert game.java_major(executable) == expected


def test_failed_repair_invalidates_success_receipt_and_retry_commits(tmp_path, monkeypatch):
    release = prepared(tmp_path)
    def fail(*args):
        raise ValueError("download failed")
    monkeypatch.setattr(game, "_worker_install", fail)
    with pytest.raises(ValueError, match="download failed"):
        game.install_game(release, tmp_path)
    assert not (tmp_path / ".launcher/game.json").exists()
    monkeypatch.setattr(game, "_worker_install", lambda *args: {"version_id": "1.21.1", "java": "java"})
    game.install_game(release, tmp_path)
    assert read_json(tmp_path / ".launcher/game.json")["runtime_key"] == game.runtime_key(release)


def test_cancellation_does_not_commit_runtime(tmp_path, monkeypatch):
    release = prepared(tmp_path)
    cancel = Event()
    def worker(*args):
        cancel.set()
        return {"version_id": "1.21.1", "java": "java"}
    monkeypatch.setattr(game, "_worker_install", worker)
    with pytest.raises(InstallCancelled):
        game.install_game(release, tmp_path, cancel=cancel)
    assert not (tmp_path / ".launcher/game.json").exists()


def test_running_game_blocks_updates_and_redacts_its_log(tmp_path, monkeypatch):
    release = prepared(tmp_path)
    player = session()
    accounts = SimpleNamespace(refresh=lambda *a, **kw: player)
    monkeypatch.setattr(game, "build_command", lambda *a, **kw: [sys.executable, "-c",
        "import time; print('SENSITIVE-GAME-TOKEN'); print('--accessToken extra-secret'); time.sleep(.3)"])
    observed = []
    def progress(event):
        observed.append(event)
        with pytest.raises(InstallConflict):
            install_release(release, tmp_path)
    result = game.play_game(release, tmp_path, accounts, player.client_id, memory_mb=4096, progress=progress)
    assert result["exit_code"] == 0 and observed[0]["phase"] == "playing"
    log = Path(result["log"]).read_text()
    assert "SENSITIVE" not in log and "extra-secret" not in log and "[redacted]" in log
    install_release(release, tmp_path)  # Game exit released the lock.


def test_launch_refresh_failure_never_spawns_game(tmp_path, monkeypatch):
    release = prepared(tmp_path)
    def refresh(*a, **kw):
        raise SignInRequired("sign in")
    monkeypatch.setattr(game.subprocess, "Popen", lambda *a, **kw: pytest.fail("Game started without authentication"))
    with pytest.raises(SignInRequired):
        game.play_game(release, tmp_path, SimpleNamespace(refresh=refresh), session().client_id, memory_mb=4096)


def test_stop_game_reaps_process_and_releases_instance(tmp_path, monkeypatch):
    release = prepared(tmp_path)
    cancel = Event()
    monkeypatch.setattr(game, "build_command", lambda *a, **kw: [sys.executable, "-c", "import time; time.sleep(60)"])
    accounts = SimpleNamespace(refresh=lambda *a, **kw: session())
    result = game.play_game(release, tmp_path, accounts, session().client_id, memory_mb=4096,
                            cancel=cancel, progress=lambda _: cancel.set())
    assert result["stopped"] and result["exit_code"] != 0
    install_release(release, tmp_path)


def test_custom_java_skips_automatic_download_and_restores_hook(tmp_path, monkeypatch):
    release = manifest()
    runtime = game.runtime_directory(tmp_path)
    folder = runtime / "versions/1.21.1"
    folder.mkdir(parents=True)
    atomic_json(folder / "1.21.1.json", {"javaVersion": {"majorVersion": 21, "component": "unavailable"}})
    def automatic(*a, **kw):
        pytest.fail("Tried to download automatic Java despite the override")
    install = SimpleNamespace(install_jvm_runtime=automatic)
    install.install_minecraft_version = lambda *a, **kw: install.install_jvm_runtime("unavailable", runtime)
    monkeypatch.setattr(runtime_worker, "launcher_library", lambda: SimpleNamespace(install=install))
    monkeypatch.setattr(runtime_worker, "java_major", lambda _: 21)
    runtime_worker.install_runtime(release, tmp_path, str(tmp_path / "custom-java.exe"))
    assert install.install_jvm_runtime is automatic
    monkeypatch.setattr(runtime_worker, "java_major", lambda _: 17)
    with pytest.raises(ValueError, match="requires at least"):
        runtime_worker.install_runtime(release.model_copy(update={"java_major": 17}), tmp_path, str(tmp_path / "custom-java.exe"))
    assert install.install_jvm_runtime is automatic


@pytest.mark.parametrize("kind", ["vanilla", "fabric", "forge", "neoforge"])
def test_runtime_installs_exact_loader_and_selects_java(tmp_path, monkeypatch, kind):
    release = manifest(kind)
    runtime = game.runtime_directory(tmp_path)
    folder = runtime / "versions/1.21.1"
    folder.mkdir(parents=True)
    atomic_json(folder / "1.21.1.json", {"javaVersion": {"majorVersion": 21, "component": "java-runtime-delta"}})
    calls = []
    def install_loader(mc, directory, **kw):
        calls.append((mc, directory, kw))
        return "1.21.1"
    library = SimpleNamespace(
        install=SimpleNamespace(install_minecraft_version=lambda *a, **kw: None),
        runtime=SimpleNamespace(get_executable_path=lambda component, directory: str(tmp_path / "java.exe")),
        mod_loader=SimpleNamespace(get_mod_loader=lambda loader: SimpleNamespace(
            get_loader_versions=lambda *a: ["1.2.3"], install=install_loader)))
    monkeypatch.setattr(runtime_worker, "launcher_library", lambda: library)
    monkeypatch.setattr(runtime_worker, "java_major", lambda path: 21)
    result = runtime_worker.install_runtime(release, tmp_path)
    assert result["java"] == str(tmp_path / "java.exe")
    assert len(calls) == (0 if kind == "vanilla" else 1)
    if calls:
        assert calls[0][2]["loader_version"] == "1.2.3"
        assert calls[0][2]["java"] == str(tmp_path / "java.exe")

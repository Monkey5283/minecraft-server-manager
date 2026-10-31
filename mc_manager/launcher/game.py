"""Install official game runtimes and supervise authenticated Java processes."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .auth import PlayerSession
from .installer import InstallCancelled, install_release, instance_lock
from .instance import atomic_json, instance_root, read_json, read_receipt, safe_target
from .models import ReleaseManifest


def launcher_library():
    try:
        import minecraft_launcher_lib
        from importlib.metadata import version
        if version("minecraft-launcher-lib") != "8.0":
            raise ImportError()
        return minecraft_launcher_lib
    except ImportError:
        raise ValueError('Install the desktop dependencies first: python -m pip install -e ".[launcher]"') from None


def process_options():
    return ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True})


def stop_process_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Kill only this launcher's child process and its installer/game children.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=20, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def runtime_key(manifest):
    data = {"minecraft": manifest.minecraft_version, "loader": manifest.loader.model_dump(), "java": manifest.java_major}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def runtime_directory(root):
    return safe_target(root, ".launcher/minecraft")


def audit_runtime(directory):
    """Reject pre-existing redirects outside our runtime, including junctions."""
    directory = instance_root(directory)
    for current, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            if not (Path(current) / name).resolve().is_relative_to(directory):
                raise ValueError("A Minecraft runtime path points outside this instance. Remove the link before repairing.")


def java_major(executable):
    executable = Path(executable)
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("Java was not found. Choose a Java executable or install with automatic Java.")
    try:
        result = subprocess.run([str(executable), "-version"], capture_output=True, text=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("The selected Java executable could not be started.") from None
    match = re.search(r'(?:openjdk|java) version "(?:1\.)?(\d+)', result.stderr + result.stdout)
    if result.returncode or not match:
        raise ValueError("The selected executable did not report a supported Java version.")
    return int(match.group(1))


def _worker_install(manifest, root, java, cancel, progress):
    launcher_library()
    # Frozen builds dispatch a worker mode through their own executable.
    python = Path(sys.executable)
    if getattr(sys, "frozen", False):
        command = [str(python), "--runtime-worker"]
    else:
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        command = [str(python), "-m", "mc_manager.launcher.runtime_worker"]
    process = subprocess.Popen(command,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **process_options())
    messages = queue.Queue()
    def reader():
        try:
            for line in process.stdout:
                if line.startswith("MONKEYCRAFT:"):
                    try:
                        messages.put(json.loads(line[len("MONKEYCRAFT:"):]))
                    except ValueError:
                        pass
        finally:
            messages.put(None)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    result = None
    error = None
    try:
        process.stdin.write(json.dumps({"manifest": manifest.model_dump(), "root": str(root), "java": java}) + "\n")
        process.stdin.close()
        ended = False
        while not ended or process.poll() is None:
            if cancel and cancel.is_set():
                raise InstallCancelled("Minecraft installation cancelled. Use Install / repair to resume verification.")
            try:
                event = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if event is None:
                ended = True
            elif event.get("type") == "result":
                result = event["result"]
            elif event.get("type") == "error":
                error = event["message"]
            elif event.get("type") == "progress" and progress:
                progress(event["event"])
        if process.wait() or not result:
            raise ValueError(error or "Minecraft installation failed. Check your connection and retry Install / repair.")
        return result
    finally:
        stop_process_tree(process)
        thread.join(timeout=5)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def install_game(manifest: ReleaseManifest, instance: Path, *, java="", cancel=None, progress=None):
    with instance_lock(instance) as root:
        if safe_target(root, ".launcher/transaction.json").exists():
            raise ValueError("Recover the interrupted pack update before installing Minecraft.")
        if cancel and cancel.is_set():
            raise InstallCancelled("Installation cancelled.")
        runtime = runtime_directory(root)
        runtime.mkdir(parents=True, exist_ok=True)
        audit_runtime(runtime)
        receipt = safe_target(root, ".launcher/game.json")
        # An interrupted repair must not leave an old success receipt launchable.
        receipt.unlink(missing_ok=True)
        result = _worker_install(manifest, root, java, cancel, progress)
        if cancel and cancel.is_set():
            raise InstallCancelled("Minecraft installation cancelled. Run Install / repair before playing.")
        atomic_json(receipt, {"schema_version": 1, "runtime_key": runtime_key(manifest), **result})
        return result


def install_client(manifest, instance, *, java="", cancel=None, progress=None, **pack_options):
    launcher_library()  # Fail before modifying the pack if dependencies are missing.
    pack = install_release(manifest, instance, cancel=cancel, progress=progress, **pack_options)
    game = install_game(manifest, instance, java=java, cancel=cancel, progress=progress)
    return {**pack, "game": game, "game_installed": True,
            "remaining_steps": ["Sign in with Microsoft and press Play"]}


def installed_game(manifest, root):
    receipt = read_receipt(root)
    if not receipt or receipt.manifest != manifest:
        raise ValueError("Install the selected pack release before playing.")
    if safe_target(root, ".launcher/transaction.json").exists():
        raise ValueError("Recover the interrupted pack update before playing.")
    path = safe_target(root, ".launcher/game.json")
    if not path.exists():
        raise ValueError("Install / repair Minecraft before playing.")
    data = read_json(path)
    if data.get("schema_version") != 1 or data.get("runtime_key") != runtime_key(manifest):
        raise ValueError("The installed Minecraft runtime does not match this release. Run Install / repair.")
    return data


def build_command(manifest, root, session: PlayerSession, *, memory_mb, java=""):
    session.require_valid()
    if type(memory_mb) is not int or not 512 <= memory_mb <= 65536:
        raise ValueError("Choose a memory limit between 512 and 65536 MiB.")
    data = installed_game(manifest, root)
    runtime = runtime_directory(root)
    audit_runtime(runtime)
    lib = launcher_library()
    expected = (manifest.minecraft_version if manifest.loader.kind == "vanilla" else
                lib.mod_loader.get_mod_loader(manifest.loader.kind).get_installed_version(manifest.minecraft_version, manifest.loader.version))
    if data.get("version_id") != expected:
        raise ValueError("The installed loader receipt is invalid. Run Install / repair.")
    executable = java or data["java"]
    if java_major(executable) != manifest.java_major:
        raise ValueError(f"This profile needs Java {manifest.java_major}. Choose that Java version or use automatic Java.")
    options = {"username": session.name, "uuid": session.uuid, "token": session.access_token,
               "executablePath": executable, "gameDirectory": str(root),
               "jvmArguments": [f"-Xmx{memory_mb}M", "-Xms512M"],
               "launcherName": "Monkeycraft", "launcherVersion": "0.1.0", "enableLoggingConfig": True}
    command = lib.command.get_minecraft_command(expected, runtime, options)
    command = [part.replace("${clientid}", session.client_id).replace("${auth_xuid}", session.xuid) for part in command]
    if any("${" in argument for argument in command):
        raise ValueError("This Minecraft version has unsupported launch arguments. Update the launcher.")
    # Missing libraries must fail before a process with the player's token starts.
    for flag in ("-cp", "-classpath", "--class-path"):
        if flag in command:
            for entry in command[command.index(flag) + 1].split(os.pathsep):
                if not Path(entry).is_file():
                    raise ValueError("A required game library is missing. Run Install / repair.")
    return command


def redact_output(line, token):
    line = line.replace(token, "[redacted]")
    return re.sub(r'(?i)(--accessToken\s+|access_token[\s:=]+|Bearer\s+)\S+', r'\1[redacted]', line)


def play_game(manifest, instance, accounts, client_id, *, memory_mb, java="", cancel=None, progress=None):
    """Hold the same OS lock as pack installation for the entire game lifetime."""
    with instance_lock(instance) as root:
        installed_game(manifest, root)
        if cancel and cancel.is_set():
            raise InstallCancelled("Launch cancelled.")
        session = accounts.refresh(client_id, cancel=cancel)
        command = build_command(manifest, root, session, memory_mb=memory_mb, java=java)
        if cancel and cancel.is_set():
            raise InstallCancelled("Launch cancelled.")
        log_path = safe_target(root, ".launcher/game-output.log")
        # Never log the command; it includes the short-lived game access token.
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, **process_options())
        def collect():
            log = None
            try:
                try:
                    log = log_path.open("w", encoding="utf-8")
                except OSError:
                    pass
                skipping = False
                for raw in iter(lambda: process.stdout.readline(65536), b""):
                    # Discard overlong lines entirely, including tokens split across chunks.
                    if skipping or (len(raw) == 65536 and not raw.endswith(b"\n")):
                        skipping = not raw.endswith(b"\n")
                        continue
                    line = redact_output(raw.decode("utf-8", errors="replace"), session.access_token)
                    try:
                        if log and log.tell() < 8 * 1024 * 1024:
                            log.write(line)
                            log.flush()
                    except OSError:
                        log.close()
                        log = None
            except OSError:
                pass
            finally:
                if log:
                    log.close()
        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            if progress:
                progress({"phase": "playing", "message": f"Minecraft is running as {session.name}. Close the game to return to the launcher.",
                          "completed": 1, "total": 1})
            while process.poll() is None:
                if cancel and cancel.wait(0.2):
                    stop_process_tree(process)
                    break
                if cancel is None:
                    time.sleep(0.2)
            return {"exit_code": process.wait(), "log": str(log_path), "stopped": bool(cancel and cancel.is_set())}
        finally:
            stop_process_tree(process)
            reader.join(timeout=5)
            process.stdout.close()

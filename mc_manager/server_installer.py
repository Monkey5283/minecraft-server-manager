from __future__ import annotations

import argparse
import json
import os
import pwd
import grp
import re
import shutil
import shlex
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .paper_download import USER_AGENT
from .server_catalog import (
    CatalogError,
    DownloadSpec,
    digest_file,
    resolve_forge,
    resolve_neoforge,
    resolve_paper,
    resolve_vanilla,
)


ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$")
MEMORY_RE = re.compile(r"^[1-9][0-9]{0,5}[MG]$")
SERVER_TYPES = frozenset({"vanilla", "paper", "forge", "neoforge"})
MAX_SERVER_DOWNLOAD = 1024 * 1024 * 1024


class InstallError(RuntimeError):
    pass


def _download(spec: DownloadSpec, destination: Path) -> None:
    request = Request(spec.url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=60) as response, destination.open("wb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_SERVER_DOWNLOAD:
                    raise InstallError("Server download exceeded the 1 GiB safety limit")
                output.write(chunk)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise InstallError(f"Could not download the server: {exc}") from exc
    actual = digest_file(str(destination), spec.checksum_algorithm)
    if actual != spec.checksum:
        raise InstallError("Downloaded server failed its publisher checksum")


def _resolve(server_type: str, version: str) -> DownloadSpec:
    try:
        if server_type == "vanilla":
            return resolve_vanilla(version)
        if server_type == "paper":
            return resolve_paper(version)
        if server_type == "forge":
            return resolve_forge(version)
        if server_type == "neoforge":
            return resolve_neoforge(version)
    except CatalogError as exc:
        raise InstallError(str(exc)) from exc
    raise InstallError("Unsupported server type")


def _validate_java(java_path: str, required_major: int | None) -> Path:
    path = Path(java_path)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise InstallError(f"Java executable does not exist: {java_path}")
    try:
        result = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"Could not run Java: {exc}") from exc
    output = result.stderr + result.stdout
    match = re.search(r'version "(?:1\.)?([0-9]+)', output)
    installed_major = int(match.group(1)) if match else None
    if result.returncode != 0 or installed_major is None:
        raise InstallError("Could not determine the installed Java version")
    if required_major is not None and installed_major < required_major:
        raise InstallError(
            f"This Minecraft version needs Java {required_major} or newer, "
            f"but {java_path} is Java {installed_major}"
        )
    return path


def _write(path: Path, content: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)


def _memory_megabytes(value: str) -> int:
    amount = int(value[:-1])
    return amount * 1024 if value.endswith("G") else amount


def _load_registry(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "servers": []}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"Could not read managed server registry: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("servers"), list):
        raise InstallError("Managed server registry has an invalid format")
    return payload


def _server_record(
    server_id: str,
    name: str,
    port: int,
    server_type: str,
) -> dict:
    service = f"minecraft@{server_id}.service"
    managed_action = "/opt/minecraft-manager/venv/bin/mc-manager-managed-action"
    actions: dict[str, list[list[str]]] = {
        "start": [["sudo", "-n", managed_action, "start", server_id]],
        "stop": [["sudo", "-n", managed_action, "stop", server_id]],
        "restart": [["sudo", "-n", managed_action, "restart", server_id]],
        "status": [["/usr/bin/systemctl", "is-active", "--quiet", service]],
    }
    if server_type == "paper":
        actions["update"] = [[
            "sudo", "-n", managed_action, "update", server_id
        ]]
    return {
        "id": server_id,
        "name": name,
        "working_directory": f"/srv/minecraft/{server_id}",
        "timeout_seconds": 120,
        "update_timeout_seconds": 1800,
        "player_query": {"host": "127.0.0.1", "port": port, "timeout_seconds": 3},
        "file_manager": {
            "enabled": True,
            "root": f"/srv/minecraft/{server_id}",
            "max_edit_size_bytes": 2097152,
            "max_upload_size_bytes": 134217728,
        },
        "actions": actions,
        "scripts": {
            "backup": [["sudo", "-n", managed_action, "backup", server_id]]
        },
    }


def install_server(request: dict) -> dict:
    if os.geteuid() != 0:
        raise InstallError("Server provisioning must run as root")
    server_id = str(request.get("id", ""))
    name = str(request.get("name", "")).strip()
    server_type = str(request.get("type", ""))
    version = str(request.get("version", ""))
    minimum_memory = str(request.get("minimum_memory", "1G")).upper()
    maximum_memory = str(request.get("maximum_memory", "4G")).upper()
    java_path = str(request.get("java_path", "/usr/bin/java"))
    port = request.get("port", 25565)
    if not ID_RE.fullmatch(server_id):
        raise InstallError("Server id must use lowercase letters, numbers, '-' or '_'")
    if not name or len(name) > 128 or any(character in name for character in "\r\n"):
        raise InstallError("Server name must be between 1 and 128 characters")
    if server_type not in SERVER_TYPES:
        raise InstallError("Unsupported server type")
    if not VERSION_RE.fullmatch(version):
        raise InstallError("Server version has an invalid format")
    if not MEMORY_RE.fullmatch(minimum_memory) or not MEMORY_RE.fullmatch(maximum_memory):
        raise InstallError("Memory values must look like 1G, 4096M, or similar")
    if _memory_megabytes(minimum_memory) > _memory_megabytes(maximum_memory):
        raise InstallError("Minimum memory cannot be greater than maximum memory")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise InstallError("Server port must be between 1024 and 65535")
    if request.get("accept_eula") is not True:
        raise InstallError("The Minecraft EULA must be accepted before installation")

    registry_path = Path("/srv/minecraft/.manager/managed-servers.json")
    registry = _load_registry(registry_path)
    if any(item.get("id") == server_id for item in registry["servers"]):
        raise InstallError(f"Server id already exists: {server_id}")
    if any(
        isinstance(item.get("player_query"), dict)
        and item["player_query"].get("port") == port
        for item in registry["servers"]
        if isinstance(item, dict)
    ):
        raise InstallError(f"Server port is already assigned: {port}")
    final_dir = Path("/srv/minecraft") / server_id
    if final_dir.exists():
        raise InstallError(f"Server directory already exists: {final_dir}")

    spec = _resolve(server_type, version)
    java = _validate_java(java_path, spec.java_major)
    Path("/srv/minecraft").mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{server_id}-install-", dir="/srv/minecraft"))
    final_created = False
    registry_written = False
    try:
        if server_type in {"vanilla", "paper"}:
            _download(spec, staging / "server.jar")
        else:
            installer = staging / f"{server_type}-installer.jar"
            _download(spec, installer)
            try:
                completed = subprocess.run(
                    [str(java), "-jar", str(installer), "--installServer"],
                    cwd=staging,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise InstallError(f"Could not run the {server_type} installer: {exc}") from exc
            if completed.returncode != 0:
                detail = (completed.stdout + completed.stderr)[-4000:]
                raise InstallError(f"{server_type} installer failed:\n{detail}")
            installer.unlink(missing_ok=True)

        if server_type in {"forge", "neoforge"} and not (staging / "run.sh").exists():
            jars = sorted(
                path for path in staging.glob("*.jar")
                if "installer" not in path.name and "shim" not in path.name
            )
            if not jars:
                raise InstallError(f"{server_type} installer did not create a runnable server")
            shutil.copy2(jars[-1], staging / "server.jar")

        if (staging / "run.sh").exists():
            os.chmod(staging / "run.sh", 0o750)
            launch = "#!/usr/bin/env bash\nset -euo pipefail\nexec ./run.sh nogui\n"
            _write(
                staging / "user_jvm_args.txt",
                f"-Xms{minimum_memory}\n-Xmx{maximum_memory}\n",
                0o640,
            )
        else:
            launch = (
                "#!/usr/bin/env bash\nset -euo pipefail\n"
                f'exec {shlex.quote(str(java))} -Xms{minimum_memory} -Xmx{maximum_memory} '
                "-jar server.jar nogui\n"
            )
        _write(staging / "start-server", launch, 0o750)
        _write(staging / "eula.txt", "eula=true\n", 0o640)
        _write(
            staging / "server.properties",
            f"server-port={port}\nenable-query=true\nquery.port={port}\n",
            0o640,
        )
        staging.replace(final_dir)
        final_created = True

        try:
            minecraft_user = pwd.getpwnam("minecraft")
            minecraft_group = grp.getgrnam("minecraft")
        except KeyError as exc:
            raise InstallError(
                "The minecraft service account is missing; rerun the agent installer"
            ) from exc
        for root, directories, files in os.walk(final_dir):
            os.chown(root, minecraft_user.pw_uid, minecraft_group.gr_gid)
            for item in directories + files:
                os.chown(Path(root) / item, minecraft_user.pw_uid, minecraft_group.gr_gid)

        if server_type == "paper":
            _write(
                final_dir / ".manager-update.env",
                (
                    "UPDATE_PROVIDER=paper\n"
                    f"PAPER_VERSION={version}\n"
                    f"SERVER_DIR=/srv/minecraft/{server_id}\n"
                    "JAR_NAME=server.jar\n"
                    f"SERVICE_NAME=minecraft@{server_id}.service\n"
                ),
                0o600,
            )

        subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)
        subprocess.run(
            ["/usr/bin/systemctl", "enable", f"minecraft@{server_id}.service"],
            check=True,
        )
        registry["servers"].append(_server_record(server_id, name, port, server_type))
        _write(registry_path, json.dumps(registry, indent=2) + "\n", 0o640)
        registry_written = True
        try:
            os.chown(registry_path, 0, grp.getgrnam("mcmanager").gr_gid)
        except KeyError:
            pass
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if final_created:
            subprocess.run(
                ["/usr/bin/systemctl", "disable", f"minecraft@{server_id}.service"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            shutil.rmtree(final_dir, ignore_errors=True)
        if registry_written:
            registry["servers"] = [
                item for item in registry["servers"] if item.get("id") != server_id
            ]
            try:
                _write(registry_path, json.dumps(registry, indent=2) + "\n", 0o640)
            except OSError:
                pass
        raise

    return {
        "id": server_id,
        "name": name,
        "type": server_type,
        "version": version,
        "port": port,
        "state": "installed",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision one managed Minecraft server")
    parser.add_argument("--request", required=True, help="Validated JSON provisioning request")
    args = parser.parse_args()
    try:
        request = json.loads(args.request)
        if not isinstance(request, dict):
            raise InstallError("Provisioning request must be a JSON object")
        result = install_server(request)
    except (json.JSONDecodeError, InstallError, subprocess.SubprocessError, OSError) as exc:
        raise SystemExit(f"Provisioning failed: {exc}") from exc
    print(json.dumps(result))


def managed_action_main() -> None:
    parser = argparse.ArgumentParser(description="Run one allowlisted managed server action")
    parser.add_argument("action", choices=("start", "stop", "restart", "update", "backup"))
    parser.add_argument("server_id")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("managed actions must run as root")
    if not ID_RE.fullmatch(args.server_id):
        raise SystemExit("invalid server id")
    registry = _load_registry(Path("/srv/minecraft/.manager/managed-servers.json"))
    server = next(
        (item for item in registry["servers"] if item.get("id") == args.server_id),
        None,
    )
    if server is None:
        raise SystemExit("server is not in the managed registry")
    if args.action in {"start", "stop", "restart"}:
        command = [
            "/usr/bin/systemctl",
            args.action,
            f"minecraft@{args.server_id}.service",
        ]
    elif args.action == "backup":
        command = ["/usr/local/sbin/backup-minecraft", args.server_id]
    else:
        if "update" not in server.get("actions", {}):
            raise SystemExit("updates are not configured for this server")
        command = ["/usr/local/sbin/update-minecraft-jar", args.server_id]
    completed = subprocess.run(command, check=False)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()

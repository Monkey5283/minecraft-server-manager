from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import shlex
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - Linux-only helpers are mocked on Windows
    grp = None
    pwd = None

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
MINECRAFT_ROOT = Path("/srv/minecraft")
BACKUP_ROOT = Path("/srv/minecraft-backups")
REGISTRY_PATH = MINECRAFT_ROOT / ".manager" / "managed-servers.json"


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
    *,
    version: str,
    java_path: str,
    minimum_memory: str,
    maximum_memory: str,
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
        "console": {
            "enabled": True,
            "input_pipe": f"/srv/minecraft/{server_id}/.manager/console.in",
            "log_file": f"/srv/minecraft/{server_id}/logs/latest.log",
            "max_command_bytes": 1024,
            "max_output_bytes": 262144,
        },
        "actions": actions,
        "scripts": {
            "backup": [["sudo", "-n", managed_action, "backup", server_id]]
        },
        "software": {
            "type": server_type,
            "version": version,
            "java_path": java_path,
            "minimum_memory": minimum_memory,
            "maximum_memory": maximum_memory,
        },
    }


def _stage_software(
    staging: Path,
    server_type: str,
    spec: DownloadSpec,
    java: Path,
    minimum_memory: str,
    maximum_memory: str,
) -> None:
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
            path
            for path in staging.glob("*.jar")
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


def _minecraft_identity() -> tuple[int, int]:
    if pwd is None or grp is None:
        raise InstallError("Minecraft identity lookup is only available on Linux")
    try:
        minecraft_user = pwd.getpwnam("minecraft")
        minecraft_group = grp.getgrnam("minecraft")
    except KeyError as exc:
        raise InstallError(
            "The minecraft service account is missing; rerun the agent installer"
        ) from exc
    return minecraft_user.pw_uid, minecraft_group.gr_gid


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    for current_root, directories, files in os.walk(root):
        os.chown(current_root, uid, gid)
        os.chmod(current_root, 0o2770)
        for item in directories + files:
            path = Path(current_root) / item
            if path.is_symlink() and hasattr(os, "lchown"):
                os.lchown(path, uid, gid)
            else:
                os.chown(path, uid, gid)


def _write_registry(path: Path, registry: dict) -> None:
    _write(path, json.dumps(registry, indent=2) + "\n", 0o640)
    if grp is None:
        return
    try:
        os.chown(path, 0, grp.getgrnam("mcmanager").gr_gid)
    except KeyError:
        pass


def _systemctl(action: str, service: str, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/systemctl", action, service],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=check,
    )


def _service_active(service: str) -> bool:
    return _systemctl("is-active", service, check=False).returncode == 0


def _full_server_backup(server_id: str, server_dir: Path, label: str) -> Path:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_dir = BACKUP_ROOT / server_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / f"{server_id}-{label}-{timestamp}.tar.gz"
    suffix = 1
    while archive.exists():
        archive = backup_dir / (
            f"{server_id}-{label}-{timestamp}-{suffix}.tar.gz"
        )
        suffix += 1
    try:
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(
                server_dir,
                arcname=server_id,
                recursive=True,
                filter=lambda member: (
                    None
                    if member.isfifo() or member.ischr() or member.isblk()
                    else member
                ),
            )
        os.chmod(archive, 0o640)
    except (OSError, tarfile.TarError) as exc:
        archive.unlink(missing_ok=True)
        raise InstallError(f"Could not create the pre-change backup: {exc}") from exc
    return archive


def _software_backup(server_id: str, server_dir: Path) -> Path:
    return _full_server_backup(server_id, server_dir, "before-software-change")


def _deletion_backup(server_id: str, server_dir: Path) -> Path:
    return _full_server_backup(server_id, server_dir, "before-delete")


def _restore_software_backup(server_id: str, server_dir: Path, archive: Path) -> None:
    failed_dir = server_dir.with_name(f".{server_id}-failed-software-change")
    suffix = 1
    while failed_dir.exists():
        failed_dir = server_dir.with_name(
            f".{server_id}-failed-software-change-{suffix}"
        )
        suffix += 1
    server_dir.replace(failed_dir)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            prefix = f"{server_id}/"
            if any(
                member.name != server_id
                and not member.name.startswith(prefix)
                for member in members
            ):
                raise InstallError("Pre-change backup contained an invalid path")
            bundle.extractall(MINECRAFT_ROOT, filter="data")
    except Exception:
        if server_dir.exists():
            shutil.rmtree(server_dir, ignore_errors=True)
        if failed_dir.exists():
            failed_dir.replace(server_dir)
        raise
    minecraft_uid, minecraft_gid = _minecraft_identity()
    _chown_tree(server_dir, minecraft_uid, minecraft_gid)
    protected_update_environment = server_dir / ".manager-update.env"
    if protected_update_environment.is_file():
        os.chown(protected_update_environment, 0, 0)
        os.chmod(protected_update_environment, 0o600)
    shutil.rmtree(failed_dir)


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

    registry_path = REGISTRY_PATH
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
    final_dir = MINECRAFT_ROOT / server_id
    if final_dir.exists():
        raise InstallError(f"Server directory already exists: {final_dir}")

    spec = _resolve(server_type, version)
    java = _validate_java(java_path, spec.java_major)
    MINECRAFT_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{server_id}-install-", dir=MINECRAFT_ROOT))
    final_created = False
    registry_written = False
    try:
        _stage_software(
            staging,
            server_type,
            spec,
            java,
            minimum_memory,
            maximum_memory,
        )
        _write(staging / "eula.txt", "eula=true\n", 0o640)
        _write(
            staging / "server.properties",
            f"server-port={port}\nenable-query=true\nquery.port={port}\n",
            0o640,
        )
        staging.replace(final_dir)
        final_created = True

        minecraft_uid, minecraft_gid = _minecraft_identity()
        _chown_tree(final_dir, minecraft_uid, minecraft_gid)

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
        registry["servers"].append(
            _server_record(
                server_id,
                name,
                port,
                server_type,
                version=version,
                java_path=str(java),
                minimum_memory=minimum_memory,
                maximum_memory=maximum_memory,
            )
        )
        _write_registry(registry_path, registry)
        registry_written = True
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
                _write_registry(registry_path, registry)
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


def change_server_software(request: dict) -> dict:
    if os.geteuid() != 0:
        raise InstallError("Changing server software must run as root")
    server_id = str(request.get("id", ""))
    server_type = str(request.get("type", ""))
    version = str(request.get("version", ""))
    minimum_memory = str(request.get("minimum_memory", "1G")).upper()
    maximum_memory = str(request.get("maximum_memory", "4G")).upper()
    java_path = str(request.get("java_path", "/usr/bin/java"))
    if not ID_RE.fullmatch(server_id):
        raise InstallError("Server id must use lowercase letters, numbers, '-' or '_'")
    if server_type not in SERVER_TYPES:
        raise InstallError("Unsupported server type")
    if not VERSION_RE.fullmatch(version):
        raise InstallError("Server version has an invalid format")
    if not MEMORY_RE.fullmatch(minimum_memory) or not MEMORY_RE.fullmatch(maximum_memory):
        raise InstallError("Memory values must look like 1G, 4096M, or similar")
    if _memory_megabytes(minimum_memory) > _memory_megabytes(maximum_memory):
        raise InstallError("Minimum memory cannot be greater than maximum memory")
    if request.get("accept_eula") is not True:
        raise InstallError("The Minecraft EULA must be accepted before installation")
    if request.get("confirm_backup") is not True:
        raise InstallError("The backup and restart confirmation is required")

    registry = _load_registry(REGISTRY_PATH)
    record = next(
        (
            item
            for item in registry["servers"]
            if isinstance(item, dict) and item.get("id") == server_id
        ),
        None,
    )
    if record is None:
        raise InstallError(
            "Only servers provisioned by the dashboard can change software"
        )
    server_dir = MINECRAFT_ROOT / server_id
    if not server_dir.is_dir():
        raise InstallError(f"Server directory does not exist: {server_dir}")

    spec = _resolve(server_type, version)
    java = _validate_java(java_path, spec.java_major)
    MINECRAFT_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{server_id}-software-", dir=MINECRAFT_ROOT)
    )
    service = f"minecraft@{server_id}.service"
    original_registry = json.loads(json.dumps(registry))
    backup: Path | None = None
    was_active = False
    changed = False
    try:
        _stage_software(
            staging,
            server_type,
            spec,
            java,
            minimum_memory,
            maximum_memory,
        )
        minecraft_uid, minecraft_gid = _minecraft_identity()
        _chown_tree(staging, minecraft_uid, minecraft_gid)

        was_active = _service_active(service)
        _systemctl("stop", service)
        backup = _software_backup(server_id, server_dir)

        changed = True
        shutil.copytree(staging, server_dir, dirs_exist_ok=True)
        update_environment = server_dir / ".manager-update.env"
        if server_type == "paper":
            _write(
                update_environment,
                (
                    "UPDATE_PROVIDER=paper\n"
                    f"PAPER_VERSION={version}\n"
                    f"SERVER_DIR={server_dir}\n"
                    "JAR_NAME=server.jar\n"
                    f"SERVICE_NAME={service}\n"
                ),
                0o600,
            )
        else:
            update_environment.unlink(missing_ok=True)

        actions = dict(record.get("actions", {}))
        managed_action = "/opt/minecraft-manager/venv/bin/mc-manager-managed-action"
        if server_type == "paper":
            actions["update"] = [[
                "sudo", "-n", managed_action, "update", server_id
            ]]
        else:
            actions.pop("update", None)
        record["actions"] = actions
        record["software"] = {
            "type": server_type,
            "version": version,
            "java_path": str(java),
            "minimum_memory": minimum_memory,
            "maximum_memory": maximum_memory,
        }
        _write_registry(REGISTRY_PATH, registry)

        if was_active:
            _systemctl("start", service)
            time.sleep(8)
            if not _service_active(service):
                raise InstallError(
                    "The changed server did not stay running; restoring its backup"
                )
    except Exception as exc:
        if backup is not None and changed:
            try:
                _systemctl("stop", service, check=False)
                _restore_software_backup(server_id, server_dir, backup)
                _write_registry(REGISTRY_PATH, original_registry)
                if was_active:
                    _systemctl("start", service)
            except Exception as rollback_exc:
                raise InstallError(
                    f"Software change failed ({exc}); automatic rollback also failed: "
                    f"{rollback_exc}. Backup: {backup}"
                ) from rollback_exc
        elif was_active:
            _systemctl("start", service, check=False)
        if isinstance(exc, InstallError):
            raise
        raise InstallError(f"Could not change server software: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "id": server_id,
        "type": server_type,
        "version": version,
        "backup": str(backup),
        "state": "changed",
        "restarted": was_active,
    }


def delete_managed_server(request: dict) -> dict:
    if os.geteuid() != 0:
        raise InstallError("Deleting a managed server must run as root")
    server_id = str(request.get("id", ""))
    confirmation = str(request.get("confirmation", ""))
    if not ID_RE.fullmatch(server_id):
        raise InstallError("Server id must use lowercase letters, numbers, '-' or '_'")
    if confirmation != server_id:
        raise InstallError("Deletion confirmation must exactly match the server id")

    registry = _load_registry(REGISTRY_PATH)
    record = next(
        (
            item
            for item in registry["servers"]
            if isinstance(item, dict) and item.get("id") == server_id
        ),
        None,
    )
    if record is None:
        raise InstallError("Only dashboard-provisioned servers can be deleted")
    server_dir = MINECRAFT_ROOT / server_id
    if not server_dir.is_dir():
        raise InstallError(f"Server directory does not exist: {server_dir}")

    service = f"minecraft@{server_id}.service"
    was_active = _service_active(service)
    backup: Path | None = None
    quarantine = MINECRAFT_ROOT / f".{server_id}-deleting"
    suffix = 1
    while quarantine.exists():
        quarantine = MINECRAFT_ROOT / f".{server_id}-deleting-{suffix}"
        suffix += 1
    moved = False
    registry_changed = False
    try:
        _systemctl("stop", service)
        backup = _deletion_backup(server_id, server_dir)
        _systemctl("disable", service)
        server_dir.replace(quarantine)
        moved = True
        registry["servers"] = [
            item for item in registry["servers"] if item is not record
        ]
        _write_registry(REGISTRY_PATH, registry)
        registry_changed = True
    except Exception as exc:
        if moved and quarantine.exists() and not server_dir.exists():
            quarantine.replace(server_dir)
        if registry_changed:
            registry["servers"].append(record)
            try:
                _write_registry(REGISTRY_PATH, registry)
            except OSError:
                pass
        _systemctl("enable", service, check=False)
        if was_active:
            _systemctl("start", service, check=False)
        if isinstance(exc, InstallError):
            raise
        raise InstallError(f"Could not delete managed server: {exc}") from exc

    cleanup_pending = ""
    try:
        shutil.rmtree(quarantine)
    except OSError:
        cleanup_pending = str(quarantine)
    return {
        "id": server_id,
        "state": "deleted",
        "backup": str(backup),
        "cleanup_pending": cleanup_pending,
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


def change_software_main() -> None:
    parser = argparse.ArgumentParser(
        description="Change software for one dashboard-provisioned Minecraft server"
    )
    parser.add_argument("--request", required=True, help="Validated JSON change request")
    args = parser.parse_args()
    try:
        request = json.loads(args.request)
        if not isinstance(request, dict):
            raise InstallError("Software change request must be a JSON object")
        result = change_server_software(request)
    except (json.JSONDecodeError, InstallError, subprocess.SubprocessError, OSError) as exc:
        raise SystemExit(f"Software change failed: {exc}") from exc
    print(json.dumps(result))


def delete_server_main() -> None:
    parser = argparse.ArgumentParser(
        description="Back up and delete one dashboard-provisioned Minecraft server"
    )
    parser.add_argument("--request", required=True, help="Validated JSON deletion request")
    args = parser.parse_args()
    try:
        request = json.loads(args.request)
        if not isinstance(request, dict):
            raise InstallError("Deletion request must be a JSON object")
        result = delete_managed_server(request)
    except (json.JSONDecodeError, InstallError, subprocess.SubprocessError, OSError) as exc:
        raise SystemExit(f"Server deletion failed: {exc}") from exc
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

"""Verified pack downloads, serialized installation, and crash recovery.

Only manifest-owned pack files are changed. Minecraft/runtime installation is a
separate adapter. The installed receipt is the transaction's commit point.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable, Literal
from urllib.parse import urljoin

import httpx
from pydantic import Field, field_validator, model_validator

from .client import plan_installation
from .instance import InstallReceipt, atomic_json, fingerprint, instance_root, read_json, read_receipt, safe_target
from .models import Contract, PackFile, ReleaseManifest

Progress = Callable[[dict], None]


class InstallCancelled(ValueError):
    pass


class InstallConflict(ValueError):
    pass


class Stamp(Contract):
    size: int = Field(strict=True, ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Operation(Contract):
    path: str
    before: Stamp | None
    after: Stamp | None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value):
        return PackFile.safe_path(value)


class Transaction(Contract):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    operations: list[Operation] = Field(max_length=8192)

    @model_validator(mode="after")
    def unique_operations(self):
        paths = {op.path.casefold() for op in self.operations}
        if len(paths) != len(self.operations):
            raise ValueError("Duplicate transaction targets")
        for path in paths:
            parts = path.split("/")
            if any("/".join(parts[:index]) in paths for index in range(1, len(parts))):
                raise ValueError("Transaction contains a file/directory collision")
        return self


def _cancelled(cancel: Event | None):
    if cancel and cancel.is_set():
        raise InstallCancelled("Installation cancelled; no partial pack update was kept.")


def _report(progress, **event):
    if progress:
        progress(event)


@contextmanager
def instance_lock(instance: Path):
    root = instance_root(instance)
    root.mkdir(parents=True, exist_ok=True)
    metadata = safe_target(root, ".launcher")
    metadata.mkdir(exist_ok=True)
    lock = safe_target(root, ".launcher/install.lock")
    with lock.open("a+b") as handle:
        if not lock.stat().st_size:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise InstallConflict("Another launcher operation is using this instance.") from exc
        try:
            yield root
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _copy_durable(source: Path, target: Path):
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())


def _backup_path(root, transaction, index):
    return safe_target(root, f".launcher/history/{transaction.id}/backup/{index}")


def _recover_locked(root: Path) -> dict:
    journal = safe_target(root, ".launcher/transaction.json")
    if not journal.exists():
        return {"recovered": False}
    transaction = Transaction.model_validate(read_json(journal))
    receipt = read_receipt(root)
    if receipt and receipt.transaction_id == transaction.id:
        journal.unlink()  # The receipt was committed; preserve subsequent user edits.
        return {"recovered": True, "state": "committed", "transaction_id": transaction.id}
    # Check every target and backup before touching anything during recovery.
    pending = []
    for index, op in enumerate(transaction.operations):
        target = safe_target(root, op.path)
        before = op.before.model_dump() if op.before else None
        after = op.after.model_dump() if op.after else None
        current = fingerprint(target)
        if current == before:
            continue
        if current != after:
            raise InstallConflict(f"Recovery found a newer local change in {op.path}. Backups and journal were retained.")
        if before is not None and fingerprint(_backup_path(root, transaction, index)) != before:
            raise InstallConflict(f"Recovery backup is missing or damaged for {op.path}.")
        pending.append((index, op))
    for index, op in reversed(pending):
        target = safe_target(root, op.path)
        if op.before is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".launcher-restore-{uuid.uuid4().hex}")
            try:
                _copy_durable(_backup_path(root, transaction, index), temporary)
                os.replace(temporary, safe_target(root, op.path))
            finally:
                temporary.unlink(missing_ok=True)
    journal.unlink()
    return {"recovered": True, "state": "rolled_back", "transaction_id": transaction.id}


def recover_installation(instance: Path) -> dict:
    with instance_lock(instance) as root:
        return _recover_locked(root)


def _download(client, entry: PackFile, destination: Path, cancel, progress, already, total):
    url = entry.url
    for redirect in range(6):
        _cancelled(cancel)
        with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect == 5 or "location" not in response.headers:
                    raise ValueError(f"Too many or invalid download redirects for {entry.path}")
                url = PackFile.safe_url(urljoin(url, response.headers["location"]))
                continue
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError(f"Unexpected download encoding for {entry.path}")
            digest = hashlib.sha256()
            count = 0
            with destination.open("xb") as handle:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    _cancelled(cancel)
                    count += len(chunk)
                    if count > entry.size:
                        raise ValueError(f"Download exceeds the declared size: {entry.path}")
                    handle.write(chunk)
                    digest.update(chunk)
                    _report(progress, phase="download", path=entry.path, completed=already + count, total=total)
                handle.flush()
                os.fsync(handle.fileno())
            if count != entry.size or digest.hexdigest() != entry.sha256:
                raise ValueError(f"Download failed size/SHA-256 verification: {entry.path}")
            return


def install_release(manifest: ReleaseManifest, instance: Path, *, replace_modified=False,
                    expected_plan_id: str | None = None, cancel: Event | None = None,
                    progress: Progress | None = None, transport=None) -> dict:
    manifest = ReleaseManifest.model_validate(manifest.model_dump())
    _cancelled(cancel)
    with instance_lock(instance) as root:
        _recover_locked(root)
        _report(progress, phase="verify", message="Checking managed files")
        plan = plan_installation(manifest, root)
        if expected_plan_id is not None and plan["plan_id"] != expected_plan_id:
            raise InstallConflict("Files changed since the preview. Check the instance again before installing.")
        if plan["conflicts"] and not replace_modified:
            raise InstallConflict("Locally changed or unowned files need approval before replacement: " + ", ".join(plan["conflicts"]))
        changes = [item for item in plan["files"] if item["action"] != "keep"]
        operations = [Operation(path=item["path"], before=item["before"],
            after={"size": item["size"], "sha256": item["sha256"]}) for item in changes]
        operations += [Operation(path=item["path"], before=item["before"], after=None) for item in plan["removals"]]
        transaction = Transaction(id=uuid.uuid4().hex, operations=operations)
        history = safe_target(root, f".launcher/history/{transaction.id}")
        required = plan["download_bytes"] + sum(op.before.size for op in operations if op.before) + 1024 * 1024
        if shutil.disk_usage(root).free < required:
            raise ValueError("Not enough free space for downloads and recovery backups")
        history.mkdir(parents=True)
        (history / "downloads").mkdir()
        (history / "backup").mkdir()
        journal = safe_target(root, ".launcher/transaction.json")
        written_journal = False
        try:
            with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False, transport=transport) as client:
                downloaded = 0
                for index, item in enumerate(changes):
                    entry = PackFile.model_validate({key: item[key] for key in ("path", "url", "size", "sha256")})
                    _download(client, entry, history / "downloads" / str(index), cancel, progress, downloaded, plan["download_bytes"])
                    downloaded += entry.size
            _cancelled(cancel)
            # Include kept files in the recheck: a running game/editor may have changed them.
            for item in [*plan["files"], *plan["removals"]]:
                if fingerprint(safe_target(root, item["path"])) != item["before"]:
                    raise InstallConflict(f"File changed during download: {item['path']}")
            for index, op in enumerate(operations):
                _cancelled(cancel)
                if op.before is not None:
                    backup = _backup_path(root, transaction, index)
                    _copy_durable(safe_target(root, op.path), backup)
                    if fingerprint(backup) != op.before.model_dump():
                        raise InstallConflict(f"File changed during backup: {op.path}")
            atomic_json(journal, transaction.model_dump())
            written_journal = True
            for index, op in enumerate(operations):
                _cancelled(cancel)
                target = safe_target(root, op.path)
                if fingerprint(target) != (op.before.model_dump() if op.before else None):
                    raise InstallConflict(f"File changed during installation: {op.path}")
                if op.after is None:
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(safe_target(root, f".launcher/history/{transaction.id}/downloads/{index}"), safe_target(root, op.path))
                _report(progress, phase="apply", path=op.path, completed=index + 1, total=len(operations))
            _cancelled(cancel)
            receipt = InstallReceipt(transaction_id=transaction.id, installed_at=datetime.now(timezone.utc).isoformat(), manifest=manifest)
            atomic_json(safe_target(root, ".launcher/installed.json"), receipt.model_dump())
            journal.unlink()
        except BaseException:
            if written_journal:
                _recover_locked(root)
            raise
        _report(progress, phase="complete", message="Pack files installed and verified")
        return {"profile_id": manifest.profile_id, "version": manifest.version, "installed": True,
                "download_bytes": plan["download_bytes"], "changed_files": len(operations),
                "backup_directory": str(history / "backup"), "launch_ready": False}

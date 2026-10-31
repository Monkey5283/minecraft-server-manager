"""Portable instance paths, receipts, and durable metadata writes."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import Contract, ReleaseManifest

METADATA_LIMIT = 16 * 1024 * 1024


class InstallReceipt(Contract):
    schema_version: Literal[1] = 1
    transaction_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    installed_at: str
    manifest: ReleaseManifest


def is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def instance_root(path: Path) -> Path:
    path = path.absolute()
    if any(is_link(item) for item in (path, *path.parents)):
        raise ValueError("The instance path must not traverse symbolic links or junctions")
    if path.exists() and not path.is_dir():
        raise ValueError("The instance path must be a directory")
    return path.resolve()


def safe_target(root: Path, relative: str) -> Path:
    """Also used for launcher-owned metadata; callers validate managed file names."""
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
        raise ValueError("Invalid relative instance path")
    target = root.joinpath(*parts)
    candidate = target
    while candidate != root:
        if is_link(candidate):
            raise ValueError(f"Path traverses a symbolic link or junction: {relative}")
        if candidate != target and candidate.exists() and not candidate.is_dir():
            raise ValueError(f"Path has a non-directory parent: {relative}")
        candidate = candidate.parent
    if not target.resolve().is_relative_to(root):
        raise ValueError("Path escapes the instance directory")
    return target


def fingerprint(path: Path) -> dict | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Expected a regular file at {path.name}")
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"File changed while being checked: {path.name}")
    return {"size": after.st_size, "sha256": digest}


def read_json(path: Path) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(METADATA_LIMIT + 1)
    if len(raw) > METADATA_LIMIT:
        raise ValueError("Launcher metadata exceeds the size limit")
    return json.loads(raw)


def read_receipt(root: Path) -> InstallReceipt | None:
    path = safe_target(root, ".launcher/installed.json")
    if not path.exists():
        return None
    return InstallReceipt.model_validate(read_json(path))


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write((json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

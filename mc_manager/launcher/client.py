"""UI-independent catalog reader and read-only managed-file verification.

The desktop app consumes these contracts without panel credentials.
This module does not install Java/loaders, authenticate Microsoft, or launch a JVM.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .models import Catalog, ReleaseManifest
from .instance import fingerprint, instance_root, read_receipt, safe_target

MAX_CATALOG_BYTES = 8 * 1024 * 1024


def read_catalog(source: str, *, allow_http=False) -> Catalog:
    if source.startswith(("https://", "http://")):
        parsed = urlsplit(source)
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("Catalog URLs must not contain credentials or fragments")
        if parsed.scheme != "https" and not allow_http:
            raise ValueError("Use HTTPS for catalogs, or --allow-http on a trusted development network")
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            with client.stream("GET", source, headers={"Accept": "application/json"}) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_CATALOG_BYTES:
                        raise ValueError("Catalog exceeds the 8 MiB size limit")
    else:
        with Path(source).open("rb") as handle:
            data = handle.read(MAX_CATALOG_BYTES + 1)
        if len(data) > MAX_CATALOG_BYTES:
            raise ValueError("Catalog exceeds the 8 MiB size limit")
    return Catalog.model_validate_json(bytes(data))


def plan_installation(manifest: ReleaseManifest, instance: Path) -> dict:
    # Revalidate even if a caller bypassed Pydantic using model_construct/model_copy.
    manifest = ReleaseManifest.model_validate(manifest.model_dump())
    root = instance_root(instance)
    if safe_target(root, ".launcher/transaction.json").exists():
        raise ValueError("An interrupted installation needs recovery. Use Recover or the recover command first.")
    receipt = read_receipt(root)
    if receipt and receipt.manifest.profile_id != manifest.profile_id:
        raise ValueError("This instance belongs to a different profile. Choose a separate directory.")
    if receipt and receipt.manifest.version == manifest.version and receipt.manifest != manifest:
        raise ValueError("This release changed without a new version. Publish a new release version in the panel.")
    previous = {item.path.casefold(): item for item in receipt.manifest.files} if receipt else {}
    files = []
    for entry in manifest.files:
        target = safe_target(root, entry.path)
        old = previous.get(entry.path.casefold())
        if old and old.path != entry.path:
            raise ValueError("Changing only the case of a managed file path is not supported")
        before = fingerprint(target)
        desired = {"size": entry.size, "sha256": entry.sha256}
        state = "download" if before is None else "keep" if before == desired else "replace"
        old_stamp = {"size": old.size, "sha256": old.sha256} if old else None
        conflict = state == "replace" and before != old_stamp
        files.append({**entry.model_dump(), "action": state, "before": before, "conflict": conflict})
    current = {item.path.casefold() for item in manifest.files}
    removals = []
    for key, old in previous.items():
        if key in current:
            continue
        before = fingerprint(safe_target(root, old.path))
        if before is not None:
            removals.append({"path": old.path, "action": "remove", "before": before,
                             "conflict": before != {"size": old.size, "sha256": old.sha256}})
    result = {"schema_version": 1, "profile_id": manifest.profile_id, "version": manifest.version,
            "instance": str(root), "minecraft_version": manifest.minecraft_version,
            "loader": manifest.loader.model_dump(), "java_major": manifest.java_major,
            "memory_mb": manifest.memory_mb, "server_address": manifest.server_address,
            "files": files, "removals": removals,
            "conflicts": [item["path"] for item in [*files, *removals] if item["conflict"]],
            "download_bytes": sum(item["size"] for item in files if item["action"] != "keep"),
            "launch_ready": False,
            "remaining_steps": ["Install or verify Minecraft, Java, and the loader",
                                "Apply managed-file changes transactionally",
                                "Authenticate with Microsoft and launch Minecraft"]}
    result["plan_id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result

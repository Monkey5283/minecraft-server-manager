"""Read-only installation summaries. These never authenticate or launch Java."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .game import installed_game
from .instance import instance_root, read_receipt, safe_target
from .models import ReleaseManifest
from .installer import InstallCancelled
from .settings import profile_directory


@dataclass(frozen=True)
class InstallationStatus:
    state: Literal["not_installed", "installed", "release_changed", "repair", "recover", "blocked"]
    label: str
    message: str
    installed_version: str | None = None

    def to_dict(self):
        return asdict(self)


def inspect_installation(manifest: ReleaseManifest, instance: Path, *, java="") -> InstallationStatus:
    """Summarize receipts and required paths without hashing files or creating dirs.

    This is a point-in-time hint, not proof of file integrity or game ownership.
    Install/Play retain their own checks and instance locks.
    """
    installed = None
    try:
        manifest = ReleaseManifest.model_validate(manifest.model_dump())
        root = instance_root(instance)
        if safe_target(root, ".launcher/transaction.json").exists():
            return InstallationStatus("recover", "Recovery needed", "Use Recover to finish an interrupted pack operation.")
        receipt = read_receipt(root)
        if receipt is None:
            return InstallationStatus("not_installed", "Not installed", "Use Install / repair to download this client.")
        installed = receipt.manifest.version
        if receipt.manifest.profile_id != manifest.profile_id:
            return InstallationStatus("blocked", "Different profile", "This folder belongs to a different profile. Choose another library.", installed)
        if installed == manifest.version and receipt.manifest != manifest:
            return InstallationStatus("blocked", "Release conflict", "The published release changed without a new version. Ask the panel administrator to publish a new version.", installed)
        if installed != manifest.version:
            return InstallationStatus("release_changed", "Release changed", f"Installed {installed}; the catalog publishes {manifest.version}. Use Update to install the published release.", installed)
        try:
            data = installed_game(manifest, root)
            version = data.get("version_id")
            if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,159}", version):
                raise ValueError("The Minecraft version record is invalid. Use Install / repair.")
            metadata = safe_target(root, f".launcher/minecraft/versions/{version}/{version}.json")
            if not metadata.is_file():
                raise ValueError("Minecraft version files are missing. Use Install / repair.")
            executable = java or data.get("java")
            if not isinstance(executable, str) or not Path(executable).is_absolute() or not Path(executable).is_file():
                raise ValueError("Java is missing. Select Java in Account / Java settings or use Install / repair.")
        except (OSError, ValueError, TypeError, AttributeError):
            # Do not expose arbitrary receipt contents in UI or diagnostic JSON.
            return InstallationStatus("repair", "Repair needed", "Minecraft or Java files are missing, incomplete, or do not match this release. Use Install / repair or check your Java selection.", installed)
        return InstallationStatus("installed", "Installed", "Installation records match this release. Check files verifies pack contents; Play checks Java and your Minecraft account.", installed)
    except (OSError, ValueError, TypeError, AttributeError):
        return InstallationStatus("blocked", "Needs attention", "The installation metadata or folder could not be read safely. Check folder access and the selected library; Recover handles interrupted updates.", installed)


def inspect_library(catalog, settings, *, cancel=None) -> dict[str, InstallationStatus]:
    result = {}
    for profile in catalog.profiles:
        if cancel and cancel.is_set():
            raise InstallCancelled("Library refresh cancelled.")
        try:
            root = profile_directory(settings, profile.id)
            result[profile.id] = inspect_installation(profile.release, root, java=settings.java_executable)
        except (OSError, ValueError):
            result[profile.id] = InstallationStatus("blocked", "Needs attention", "Choose an accessible library without symbolic links or junctions.")
    return result

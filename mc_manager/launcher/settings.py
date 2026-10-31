"""Per-user desktop preferences; no account credentials are stored here."""
from __future__ import annotations

import os
import json
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator

from .instance import atomic_json, instance_root, read_json, safe_target
from .models import Contract, Identifier


class DesktopSettings(Contract):
    schema_version: Literal[1] = 1
    catalog_source: str = Field(default="", max_length=4096)
    library_directory: str = Field(min_length=1, max_length=4096)
    allow_http: bool = False
    selected_profile: Identifier | None = None
    memory_mb: int = Field(default=4096, strict=True, ge=512, le=65536)
    microsoft_client_id: str = ""
    java_executable: str = Field(default="", max_length=4096)

    @field_validator("microsoft_client_id")
    @classmethod
    def valid_client_id(cls, value):
        if not value:
            return ""
        return str(UUID(value))

    @field_validator("java_executable")
    @classmethod
    def absolute_java(cls, value):
        if value and not Path(value).is_absolute():
            raise ValueError("Choose an absolute Java executable path, or leave it blank for automatic Java")
        return value

    @field_validator("catalog_source")
    @classmethod
    def no_url_credentials(cls, value):
        if value.startswith(("https://", "http://")):
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None or parsed.fragment:
                raise ValueError("Catalog URLs must not contain credentials or fragments")
        return value

    @field_validator("library_directory")
    @classmethod
    def absolute_library(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("The instance library must be an absolute directory")
        return value


def default_data_directory() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "MonkeycraftLauncher"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "monkeycraft-launcher"


def load_settings(directory: Path) -> DesktopSettings:
    root = instance_root(directory)
    path = safe_target(root, "settings.json")
    if not path.exists():
        defaults = {}
        if getattr(sys, "frozen", False):
            defaults = json.loads(Path(__file__).with_name("defaults.json").read_text(encoding="utf-8"))
        return DesktopSettings(library_directory=str(root / "library"), **defaults)
    settings = DesktopSettings.model_validate(read_json(path))
    # Move the original bundled catalog to our public domain. Leave custom
    # catalogs, local exports, account settings and instance paths untouched.
    if settings.catalog_source == "https://monkeycraft-launcher.giddymixer.chatgpt.site/catalog.json":
        settings.catalog_source = "https://launcher.monkeyservers.com/catalog.json"
    return settings


def save_settings(directory: Path, settings: DesktopSettings) -> None:
    root = instance_root(directory)
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(safe_target(root, "settings.json"), settings.model_dump())


def profile_directory(settings: DesktopSettings, profile_id: str) -> Path:
    # A prefix avoids Windows device filenames such as a profile ID of "con".
    from pydantic import TypeAdapter
    profile_id = TypeAdapter(Identifier).validate_python(profile_id)
    return instance_root(Path(settings.library_directory)) / "instances" / f"profile-{profile_id}"

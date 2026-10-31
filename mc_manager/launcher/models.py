"""Shared, deliberately data-only contract for the panel and launcher clients."""
from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Version = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._+\-]{0,79}$")]
MANAGED_DIRECTORIES = {"mods", "config", "defaultconfigs", "resourcepacks", "shaderpacks", "kubejs"}
RESERVED_NAMES = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProfileSettings(Contract):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    # This association stays in the administrator API; never in player catalogs.
    server_id: Identifier | None = None


class NewProfile(ProfileSettings):
    id: Identifier


class ProfileUpdate(ProfileSettings):
    expected_revision: int = Field(ge=1)


class Publication(Contract):
    version: Version | None  # None withdraws the profile from the player catalog.
    expected_revision: int = Field(ge=1)


class Loader(Contract):
    kind: Literal["vanilla", "fabric", "forge", "neoforge"] = "vanilla"
    version: Version | None = None

    @model_validator(mode="after")
    def pinned_version(self):
        if (self.kind == "vanilla") != (self.version is None):
            raise ValueError("Vanilla has no loader version; mod loaders require an exact version")
        return self


class PackFile(Contract):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    path: str = Field(min_length=1, max_length=240)
    url: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(strict=True, ge=0, le=2 * 1024**3)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        parts = value.split("/")
        if len(parts) < 2 or parts[0] not in MANAGED_DIRECTORIES:
            raise ValueError("Files must be inside mods, config, defaultconfigs, resourcepacks, shaderpacks, or kubejs")
        for part in parts:
            if (not part or part in {".", ".."} or part.endswith((".", " "))
                    or any(ord(char) < 32 or char in '<>:"\\|?*' for char in part)
                    or RESERVED_NAMES.match(part)):
                raise ValueError("Use portable relative file paths without traversal or reserved names")
        return value

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\\" in value
                or any(char.isspace() or ord(char) < 32 for char in value)):
            raise ValueError("Downloads require HTTPS URLs without credentials or fragments")
        # Accessing port also rejects invalid/out-of-range ports.
        parsed.port
        return value


class ReleaseSpec(Contract):
    version: Version
    minecraft_version: Version
    loader: Loader = Field(default_factory=Loader)
    java_major: int = Field(strict=True, ge=8, le=99)
    memory_mb: int = Field(default=4096, strict=True, ge=512, le=65536)
    server_address: str | None = Field(default=None, max_length=260)
    changelog: str = Field(default="", max_length=8000)
    files: list[PackFile] = Field(default_factory=list, max_length=4096)

    @field_validator("server_address")
    @classmethod
    def valid_address(cls, value):
        if value is None:
            return value
        if not value or not re.fullmatch(r"[a-zA-Z0-9.\-:\[\]]+", value):
            raise ValueError("Use a Minecraft hostname or IP address with an optional port")
        parsed = urlsplit("//" + value)
        if not parsed.hostname or parsed.port == 0:
            raise ValueError("Invalid Minecraft server address")
        if ":" in parsed.hostname:
            ipaddress.IPv6Address(parsed.hostname)
        elif not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                     for label in parsed.hostname.rstrip(".").split(".")):
            raise ValueError("Invalid Minecraft hostname")
        if value.endswith(":"):
            raise ValueError("A port must follow the colon")
        return value

    @model_validator(mode="after")
    def consistent_files(self):
        paths: set[str] = set()
        for item in self.files:
            key = item.path.casefold()
            if key in paths:
                raise ValueError("File paths must be unique, including on Windows")
            paths.add(key)
        for path in paths:
            parts = path.split("/")
            if any("/".join(parts[:index]) in paths for index in range(1, len(parts))):
                raise ValueError("A managed file cannot also be a directory")
        if self.loader.kind == "vanilla" and any(file.path.startswith("mods/") for file in self.files):
            raise ValueError("Vanilla profiles cannot install mods; select a mod loader")
        if sum(file.size for file in self.files) > 32 * 1024**3:
            raise ValueError("A release cannot exceed 32 GiB of managed files")
        return self


class ReleaseManifest(ReleaseSpec):
    schema_version: Literal[1] = 1
    profile_id: Identifier


class CatalogProfile(Contract):
    id: Identifier
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    release: ReleaseManifest

    @model_validator(mode="after")
    def matching_profile(self):
        if self.release.profile_id != self.id:
            raise ValueError("Release profile_id does not match its catalog entry")
        return self


class Catalog(Contract):
    schema_version: Literal[1] = 1
    profiles: list[CatalogProfile] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_profiles(self):
        if len({profile.id for profile in self.profiles}) != len(self.profiles):
            raise ValueError("Catalog profile IDs must be unique")
        return self

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .paper_download import PaperDownloadError, USER_AGENT, fetch_latest_supported_build


MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_PROJECT = "https://fill.papermc.io/v3/projects/paper"
FORGE_METADATA = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
NEOFORGE_METADATA = (
    "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
)
MAX_METADATA_BYTES = 8 * 1024 * 1024
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$")
HASH_RE = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogVersion:
    id: str
    label: str
    minecraft_version: str
    loader_version: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class DownloadSpec:
    url: str
    checksum: str
    checksum_algorithm: str
    java_major: int | None = None


def _read_url(
    url: str,
    *,
    opener: Callable[..., Any] = urlopen,
    max_bytes: int = MAX_METADATA_BYTES,
) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/xml, text/xml, text/plain",
        },
    )
    try:
        with opener(request, timeout=20) as response:
            raw = response.read(max_bytes + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise CatalogError(f"Could not contact the publisher: {exc}") from exc
    if len(raw) > max_bytes:
        raise CatalogError("Publisher metadata response was too large")
    return raw


def _json_url(url: str, *, opener: Callable[..., Any] = urlopen) -> Any:
    try:
        return json.loads(_read_url(url, opener=opener).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError("Publisher returned invalid JSON") from exc


def _maven_versions(url: str, *, opener: Callable[..., Any] = urlopen) -> list[str]:
    try:
        root = ET.fromstring(_read_url(url, opener=opener))
    except ET.ParseError as exc:
        raise CatalogError("Publisher returned invalid Maven metadata") from exc
    versions = []
    for node in root.findall("./versioning/versions/version"):
        value = (node.text or "").strip()
        if VERSION_RE.fullmatch(value):
            versions.append(value)
    if not versions:
        raise CatalogError("Publisher returned no usable versions")
    return list(reversed(versions))


def _paper_versions(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("versions"), dict):
        raise CatalogError("Paper returned invalid project metadata")
    versions: list[str] = []
    for group in payload["versions"].values():
        if isinstance(group, list):
            versions.extend(
                str(value) for value in group if VERSION_RE.fullmatch(str(value))
            )
    if not versions:
        raise CatalogError("Paper returned no usable versions")
    return versions


def _minecraft_from_neoforge(version: str) -> str:
    numeric = version.split("-", 1)[0].split(".")
    if len(numeric) < 2 or not all(part.isdigit() for part in numeric[:2]):
        return "unknown"
    major, minor = int(numeric[0]), int(numeric[1])
    if major >= 26:
        return f"{major}.{minor}"
    return f"1.{major}.{minor}" if len(numeric) >= 3 else f"1.{major}"


def list_versions(
    server_type: str,
    *,
    opener: Callable[..., Any] = urlopen,
    limit: int = 250,
) -> list[CatalogVersion]:
    if server_type == "vanilla":
        payload = _json_url(MOJANG_MANIFEST, opener=opener)
        raw_versions = payload.get("versions", []) if isinstance(payload, dict) else []
        result = [
            CatalogVersion(str(item["id"]), str(item["id"]), str(item["id"]))
            for item in raw_versions
            if isinstance(item, dict)
            and item.get("type") == "release"
            and VERSION_RE.fullmatch(str(item.get("id", "")))
        ]
    elif server_type == "paper":
        result = [
            CatalogVersion(version, version, version)
            for version in _paper_versions(_json_url(PAPER_PROJECT, opener=opener))
        ]
    elif server_type == "forge":
        result = []
        for version in _maven_versions(FORGE_METADATA, opener=opener):
            minecraft_version = version.split("-", 1)[0]
            result.append(
                CatalogVersion(version, version, minecraft_version, version)
            )
    elif server_type == "neoforge":
        result = [
            CatalogVersion(
                version,
                f"{_minecraft_from_neoforge(version)} / NeoForge {version}",
                _minecraft_from_neoforge(version),
                version,
            )
            for version in _maven_versions(NEOFORGE_METADATA, opener=opener)
        ]
    else:
        raise CatalogError("Unsupported server type")
    return result[:limit]


def _manifest_version(version: str, *, opener: Callable[..., Any] = urlopen) -> dict:
    payload = _json_url(MOJANG_MANIFEST, opener=opener)
    entries = payload.get("versions", []) if isinstance(payload, dict) else []
    metadata_url = next(
        (
            str(item.get("url"))
            for item in entries
            if isinstance(item, dict) and item.get("id") == version
        ),
        None,
    )
    parsed_metadata = urlparse(metadata_url or "")
    if not metadata_url or parsed_metadata.scheme != "https" or parsed_metadata.hostname not in {
        "piston-meta.mojang.com",
        "launchermeta.mojang.com",
    }:
        raise CatalogError(f"Minecraft version {version} was not found")
    detail = _json_url(metadata_url, opener=opener)
    if not isinstance(detail, dict):
        raise CatalogError("Mojang returned invalid version metadata")
    return detail


def resolve_vanilla(
    version: str, *, opener: Callable[..., Any] = urlopen
) -> DownloadSpec:
    detail = _manifest_version(version, opener=opener)
    server = detail.get("downloads", {}).get("server")
    if not isinstance(server, dict):
        raise CatalogError(f"Minecraft {version} has no dedicated server download")
    url = str(server.get("url", ""))
    checksum = str(server.get("sha1", ""))
    parsed_download = urlparse(url)
    if parsed_download.scheme != "https" or parsed_download.hostname not in {
        "piston-data.mojang.com",
        "launcher.mojang.com",
    }:
        raise CatalogError("Mojang returned an untrusted download URL")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", checksum):
        raise CatalogError("Mojang returned an invalid server checksum")
    java = detail.get("javaVersion", {})
    java_major = (
        int(java["majorVersion"])
        if isinstance(java, dict) and str(java.get("majorVersion", "")).isdigit()
        else None
    )
    return DownloadSpec(url, checksum.lower(), "sha1", java_major)


def resolve_java_major(
    minecraft_version: str, *, opener: Callable[..., Any] = urlopen
) -> int | None:
    java = _manifest_version(minecraft_version, opener=opener).get("javaVersion", {})
    return (
        int(java["majorVersion"])
        if isinstance(java, dict) and str(java.get("majorVersion", "")).isdigit()
        else None
    )


def resolve_paper(version: str, *, opener: Callable[..., Any] = urlopen) -> DownloadSpec:
    try:
        paper = fetch_latest_supported_build(version, opener=opener)
    except PaperDownloadError as exc:
        raise CatalogError(str(exc)) from exc
    java_major = None
    try:
        java_major = resolve_java_major(version, opener=opener)
    except CatalogError:
        pass
    return DownloadSpec(paper.url, paper.sha256, "sha256", java_major)


def _maven_download(
    base: str,
    artifact: str,
    version: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> DownloadSpec:
    if not VERSION_RE.fullmatch(version):
        raise CatalogError("Loader version has an invalid format")
    encoded = quote(version, safe="._+-")
    url = f"{base}/{encoded}/{artifact}-{encoded}-installer.jar"
    try:
        checksum = (
            _read_url(url + ".sha1", opener=opener, max_bytes=256)
            .decode("ascii", errors="strict")
            .strip()
            .split()[0]
        )
    except (UnicodeDecodeError, IndexError) as exc:
        raise CatalogError("Loader repository returned an invalid checksum") from exc
    if not re.fullmatch(r"[0-9a-fA-F]{40}", checksum):
        raise CatalogError("Loader repository returned an invalid checksum")
    return DownloadSpec(url, checksum.lower(), "sha1")


def resolve_forge(version: str, *, opener: Callable[..., Any] = urlopen) -> DownloadSpec:
    download = _maven_download(
        "https://maven.minecraftforge.net/net/minecraftforge/forge",
        "forge",
        version,
        opener=opener,
    )
    try:
        java_major = resolve_java_major(version.split("-", 1)[0], opener=opener)
    except CatalogError:
        java_major = None
    return DownloadSpec(download.url, download.checksum, download.checksum_algorithm, java_major)


def resolve_neoforge(version: str, *, opener: Callable[..., Any] = urlopen) -> DownloadSpec:
    download = _maven_download(
        "https://maven.neoforged.net/releases/net/neoforged/neoforge",
        "neoforge",
        version,
        opener=opener,
    )
    try:
        java_major = resolve_java_major(_minecraft_from_neoforge(version), opener=opener)
    except CatalogError:
        java_major = None
    return DownloadSpec(download.url, download.checksum, download.checksum_algorithm, java_major)


def digest_file(path: str, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

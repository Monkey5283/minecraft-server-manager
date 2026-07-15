from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


API_ROOT = "https://fill.papermc.io/v3"
USER_AGENT = (
    "minecraft-server-manager/0.1.0 "
    "(https://github.com/Monkey5283/minecraft-server-manager)"
)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
BUILD_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
JAR_NAME_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*\.jar$")


class PaperDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperDownload:
    version: str
    build: str
    name: str
    url: str
    sha256: str


def _require_safe_line(value: object, label: str) -> str:
    if value is None:
        raise PaperDownloadError(f"Paper API did not return a {label}")
    text = str(value)
    if not text or any(character in text for character in "\r\n\t"):
        raise PaperDownloadError(f"Paper API returned an invalid {label}")
    return text


def parse_latest_stable_build(payload: Any, version: str) -> PaperDownload:
    if not VERSION_PATTERN.fullmatch(version):
        raise PaperDownloadError("Paper version has an invalid format")
    if isinstance(payload, dict) and payload.get("ok") is False:
        message = str(payload.get("message") or "unknown Paper API error")
        raise PaperDownloadError(message[:500])
    if not isinstance(payload, list):
        raise PaperDownloadError("Paper API returned an invalid builds response")

    build_data = next(
        (
            item
            for item in payload
            if isinstance(item, dict) and item.get("channel") == "STABLE"
        ),
        None,
    )
    if build_data is None:
        raise PaperDownloadError(
            f"No stable Paper build is available for Minecraft {version}"
        )

    build = _require_safe_line(build_data.get("id"), "build ID")
    if not BUILD_PATTERN.fullmatch(build):
        raise PaperDownloadError("Paper API returned an invalid build ID")
    downloads = build_data.get("downloads")
    if not isinstance(downloads, dict):
        raise PaperDownloadError("Paper build does not contain downloads")
    server_download = downloads.get("server:default")
    if not isinstance(server_download, dict):
        raise PaperDownloadError("Paper build does not contain a server jar")

    name = _require_safe_line(server_download.get("name"), "download name")
    url = _require_safe_line(server_download.get("url"), "download URL")
    checksums = server_download.get("checksums")
    sha256 = (
        _require_safe_line(checksums.get("sha256"), "SHA-256 checksum")
        if isinstance(checksums, dict)
        else ""
    )
    if not JAR_NAME_PATTERN.fullmatch(name):
        raise PaperDownloadError("Paper API returned an invalid jar name")
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "fill-data.papermc.io":
        raise PaperDownloadError("Paper API returned an untrusted download URL")
    if not SHA256_PATTERN.fullmatch(sha256):
        raise PaperDownloadError("Paper API returned an invalid SHA-256 checksum")

    return PaperDownload(
        version=version,
        build=build,
        name=name,
        url=url,
        sha256=sha256.lower(),
    )


def fetch_latest_stable_build(
    version: str,
    *,
    opener: Callable[..., Any] = urlopen,
    timeout_seconds: float = 15,
) -> PaperDownload:
    if not VERSION_PATTERN.fullmatch(version):
        raise PaperDownloadError("Paper version has an invalid format")
    endpoint = (
        f"{API_ROOT}/projects/paper/versions/"
        f"{quote(version, safe='')}/builds"
    )
    request = Request(
        endpoint,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise PaperDownloadError(f"Could not contact the Paper API: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PaperDownloadError("Paper API response was too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperDownloadError("Paper API returned invalid JSON") from exc
    return parse_latest_stable_build(payload, version)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve the latest stable Paper build for one Minecraft version"
    )
    parser.add_argument("--version", required=True, help="Minecraft version")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        download = fetch_latest_stable_build(args.version)
    except PaperDownloadError as exc:
        raise SystemExit(f"Could not resolve Paper update: {exc}") from exc
    # The shell updater reads these as four lines. Every field is validated to
    # exclude line breaks before it reaches this interface.
    print(download.build)
    print(download.url)
    print(download.sha256)
    print(download.name)


if __name__ == "__main__":
    main()

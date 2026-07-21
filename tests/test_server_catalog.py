from __future__ import annotations

import json
from unittest.mock import Mock

from mc_manager.server_catalog import (
    FORGE_METADATA,
    MOJANG_MANIFEST,
    PAPER_PROJECT,
    VELOCITY_PROJECT,
    resolve_velocity,
    list_versions,
)


def opener_for(payloads: dict[str, bytes]):
    def open_request(request, timeout=20):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = lambda size=-1: payloads[request.full_url][:size]
        return response

    return open_request


def test_catalog_lists_official_vanilla_releases_only() -> None:
    payload = json.dumps(
        {
            "versions": [
                {"id": "26.2", "type": "release"},
                {"id": "26.2-pre1", "type": "snapshot"},
                {"id": "1.21.11", "type": "release"},
            ]
        }
    ).encode()

    versions = list_versions("vanilla", opener=opener_for({MOJANG_MANIFEST: payload}))

    assert [version.id for version in versions] == ["26.2", "1.21.11"]


def test_catalog_flattens_paper_version_groups() -> None:
    payload = json.dumps({"versions": {"26": ["26.2", "26.1"], "1.21": ["1.21.11"]}}).encode()

    versions = list_versions("paper", opener=opener_for({PAPER_PROJECT: payload}))

    assert [version.id for version in versions] == ["26.2", "26.1", "1.21.11"]


def test_catalog_lists_velocity_versions() -> None:
    payload = json.dumps(
        {"versions": {"4.0.0": ["4.1.0-SNAPSHOT", "4.0.0"]}}
    ).encode()

    versions = list_versions(
        "velocity", opener=opener_for({VELOCITY_PROJECT: payload})
    )

    assert [version.id for version in versions] == ["4.1.0-SNAPSHOT", "4.0.0"]


def test_resolve_velocity_uses_verified_papermc_download() -> None:
    endpoint = (
        "https://fill.papermc.io/v3/projects/velocity/versions/4.0.0/builds"
    )
    payload = json.dumps([
        {
            "id": "100",
            "channel": "STABLE",
            "downloads": {
                "server:default": {
                    "name": "velocity-4.0.0-100.jar",
                    "url": "https://fill-data.papermc.io/v1/objects/velocity.jar",
                    "checksums": {"sha256": "a" * 64},
                }
            },
        }
    ]).encode()

    download = resolve_velocity("4.0.0", opener=opener_for({endpoint: payload}))

    assert download.url.endswith("/velocity.jar")
    assert download.checksum == "a" * 64
    assert download.java_major == 21


def test_catalog_lists_forge_maven_versions_newest_first() -> None:
    payload = b"""<metadata><versioning><versions>
      <version>1.20.1-47.4.0</version><version>1.21.1-52.0.1</version>
    </versions></versioning></metadata>"""

    versions = list_versions("forge", opener=opener_for({FORGE_METADATA: payload}))

    assert [version.id for version in versions] == ["1.21.1-52.0.1", "1.20.1-47.4.0"]
    assert versions[0].minecraft_version == "1.21.1"

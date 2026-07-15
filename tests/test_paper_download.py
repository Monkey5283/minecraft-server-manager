from __future__ import annotations

import json
from unittest.mock import Mock
from urllib.error import URLError

import pytest

from mc_manager.paper_download import (
    PaperDownloadError,
    USER_AGENT,
    fetch_latest_stable_build,
    parse_latest_stable_build,
)


def build_payload(
    *,
    channel: str = "STABLE",
    url: str = (
        "https://fill-data.papermc.io/v1/objects/"
        + "a" * 64
        + "/paper-26.1.2-81.jar"
    ),
    sha256: str = "a" * 64,
) -> dict:
    return {
        "id": 81,
        "channel": channel,
        "downloads": {
            "server:default": {
                "name": "paper-26.1.2-81.jar",
                "url": url,
                "checksums": {"sha256": sha256},
            }
        },
    }


def test_parse_uses_first_stable_paper_build() -> None:
    payload = [
        build_payload(channel="ALPHA"),
        build_payload(),
        {**build_payload(), "id": 80},
    ]

    download = parse_latest_stable_build(payload, "26.1.2")

    assert download.version == "26.1.2"
    assert download.build == "81"
    assert download.name == "paper-26.1.2-81.jar"
    assert download.sha256 == "a" * 64


@pytest.mark.parametrize(
    ("url", "checksum", "match"),
    [
        (
            "https://example.com/paper-26.1.2-81.jar",
            "a" * 64,
            "untrusted",
        ),
        (
            "https://fill-data.papermc.io/paper-26.1.2-81.jar",
            "not-a-checksum",
            "checksum",
        ),
    ],
)
def test_parse_rejects_untrusted_metadata(
    url: str,
    checksum: str,
    match: str,
) -> None:
    with pytest.raises(PaperDownloadError, match=match):
        parse_latest_stable_build(
            [build_payload(url=url, sha256=checksum)],
            "26.1.2",
        )


def test_parse_rejects_missing_stable_build() -> None:
    with pytest.raises(PaperDownloadError, match="No stable Paper build"):
        parse_latest_stable_build(
            [build_payload(channel="ALPHA")],
            "26.1.2",
        )


def test_fetch_uses_official_endpoint_and_identifying_user_agent() -> None:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps([build_payload()]).encode()
    opener = Mock(return_value=response)

    download = fetch_latest_stable_build("26.1.2", opener=opener)

    request = opener.call_args.args[0]
    assert request.full_url.endswith(
        "/projects/paper/versions/26.1.2/builds"
    )
    assert request.get_header("User-agent") == USER_AGENT
    assert opener.call_args.kwargs["timeout"] == 15
    assert download.build == "81"


def test_fetch_wraps_network_errors() -> None:
    opener = Mock(side_effect=URLError("network down"))

    with pytest.raises(PaperDownloadError, match="Could not contact"):
        fetch_latest_stable_build("26.1.2", opener=opener)

from __future__ import annotations

import asyncio
import struct

import pytest

from mc_manager.minecraft_query import (
    MinecraftQueryProtocolError,
    MinecraftQueryTimeout,
    query_players,
)


class FakeQueryServer(asyncio.DatagramProtocol):
    def __init__(
        self,
        players: tuple[str, ...] = (),
        *,
        challenge_response: bytes | None = None,
        ignore_requests: bool = False,
    ) -> None:
        self.players = players
        self.challenge_response = challenge_response
        self.ignore_requests = ignore_requests
        self.transport: asyncio.DatagramTransport | None = None
        self.session_id: bytes | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.ignore_requests:
            return
        assert self.transport is not None
        if data[:3] == b"\xfe\xfd\x09":
            self.session_id = data[3:7]
            assert all(value <= 0x0F for value in self.session_id)
            response = self.challenge_response
            if response is None:
                response = b"\x09" + self.session_id + b"12345\x00"
            elif b"{session}" in response:
                response = response.replace(b"{session}", self.session_id)
            self.transport.sendto(response, addr)
            return

        if data[:3] == b"\xfe\xfd\x00":
            assert self.session_id is not None
            assert data[3:7] == self.session_id
            assert struct.unpack(">i", data[7:11])[0] == 12345
            assert data[11:15] == b"\x00\x00\x00\x00"
            player_data = b"".join(
                name.encode("iso-8859-1") + b"\x00" for name in self.players
            )
            response = (
                b"\x00"
                + self.session_id
                + b"splitnum\x00\x80\x00"
                + b"hostname\x00Test Server\x00"
                + b"numplayers\x00"
                + str(len(self.players)).encode("ascii")
                + b"\x00\x00\x01player_\x00\x00"
                + player_data
                + b"\x00"
            )
            self.transport.sendto(response, addr)


async def start_fake_server(
    server: FakeQueryServer,
) -> tuple[asyncio.DatagramTransport, int]:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: server,
        local_addr=("127.0.0.1", 0),
    )
    assert isinstance(transport, asyncio.DatagramTransport)
    port = transport.get_extra_info("sockname")[1]
    return transport, port


async def test_query_players_returns_case_insensitive_deduplicated_names() -> None:
    transport, port = await start_fake_server(
        FakeQueryServer(("Monkey5283", "Builder", "monkey5283"))
    )
    try:
        assert await query_players("127.0.0.1", port, 1) == (
            "Monkey5283",
            "Builder",
        )
    finally:
        transport.close()


async def test_query_players_returns_empty_tuple() -> None:
    transport, port = await start_fake_server(FakeQueryServer())
    try:
        assert await query_players("127.0.0.1", port, 1) == ()
    finally:
        transport.close()


async def test_query_players_decodes_protocol_latin1_names() -> None:
    transport, port = await start_fake_server(FakeQueryServer(("Jos\u00e9",)))
    try:
        assert await query_players("127.0.0.1", port, 1) == ("Jos\u00e9",)
    finally:
        transport.close()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (b"\x09{session}not-a-number\x00", "decimal integer"),
        (b"\x09{session}12345", "null-terminated"),
        (b"\x08{session}12345\x00", "packet type"),
        (b"\x09bad!12345\x00", "session ID"),
    ],
)
async def test_query_players_rejects_malformed_challenge(
    response: bytes,
    message: str,
) -> None:
    transport, port = await start_fake_server(
        FakeQueryServer(challenge_response=response)
    )
    try:
        with pytest.raises(MinecraftQueryProtocolError, match=message):
            await query_players("127.0.0.1", port, 1)
    finally:
        transport.close()


async def test_query_players_reports_timeout() -> None:
    transport, port = await start_fake_server(FakeQueryServer(ignore_requests=True))
    try:
        with pytest.raises(MinecraftQueryTimeout, match="timed out"):
            await query_players("127.0.0.1", port, 0.05)
    finally:
        transport.close()

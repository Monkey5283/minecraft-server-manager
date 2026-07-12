from __future__ import annotations

import asyncio
import secrets
import struct
from typing import cast


QUERY_MAGIC = b"\xfe\xfd"
HANDSHAKE_TYPE = 0x09
STAT_TYPE = 0x00
FULL_STAT_PADDING = b"\x00\x00\x00\x00"
PLAYER_SECTION = b"\x00\x01player_\x00\x00"


class MinecraftQueryError(RuntimeError):
    """Base error raised while querying a Minecraft server."""


class MinecraftQueryTimeout(MinecraftQueryError):
    """The Minecraft server did not finish the query before the timeout."""


class MinecraftQueryProtocolError(MinecraftQueryError):
    """The Minecraft server returned a malformed query response."""


class _QueryDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.responses: asyncio.Queue[bytes | BaseException] = asyncio.Queue()

    def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
        self.responses.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        self.responses.put_nowait(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            self.responses.put_nowait(exc)

    async def receive(self) -> bytes:
        response = await self.responses.get()
        if isinstance(response, BaseException):
            raise response
        return response


def _parse_challenge(response: bytes, session_id: bytes) -> int:
    if len(response) < 7:
        raise MinecraftQueryProtocolError("Challenge response is too short")
    if response[0] != HANDSHAKE_TYPE:
        raise MinecraftQueryProtocolError("Challenge response has the wrong packet type")
    if response[1:5] != session_id:
        raise MinecraftQueryProtocolError("Challenge response has the wrong session ID")

    terminator = response.find(b"\x00", 5)
    if terminator == -1:
        raise MinecraftQueryProtocolError("Challenge token is not null-terminated")
    token_bytes = response[5:terminator]
    if not token_bytes:
        raise MinecraftQueryProtocolError("Challenge token is empty")
    try:
        challenge = int(token_bytes.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MinecraftQueryProtocolError("Challenge token is not a decimal integer") from exc
    if not -(2**31) <= challenge < 2**31:
        raise MinecraftQueryProtocolError("Challenge token is outside the 32-bit range")
    return challenge


def _parse_players(response: bytes, session_id: bytes) -> tuple[str, ...]:
    if len(response) < 5:
        raise MinecraftQueryProtocolError("Full-stat response is too short")
    if response[0] != STAT_TYPE:
        raise MinecraftQueryProtocolError("Full-stat response has the wrong packet type")
    if response[1:5] != session_id:
        raise MinecraftQueryProtocolError("Full-stat response has the wrong session ID")

    section_start = response.find(PLAYER_SECTION, 5)
    if section_start == -1:
        raise MinecraftQueryProtocolError("Full-stat response has no player section")
    player_data = response[section_start + len(PLAYER_SECTION) :]
    if not player_data.endswith(b"\x00"):
        raise MinecraftQueryProtocolError("Full-stat player section is not null-terminated")

    names: list[str] = []
    seen: set[str] = set()
    for raw_name in player_data.split(b"\x00"):
        if not raw_name:
            break
        name = raw_name.decode("iso-8859-1")
        normalized = name.casefold()
        if normalized not in seen:
            seen.add(normalized)
            names.append(name)
    return tuple(names)


async def _exchange(host: str, port: int, session_id: bytes) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    transport: asyncio.DatagramTransport | None = None
    try:
        created_transport, protocol = await loop.create_datagram_endpoint(
            _QueryDatagramProtocol,
            remote_addr=(host, port),
        )
        # uvloop's UDPTransport is datagram-compatible but does not inherit
        # asyncio.DatagramTransport, so rely on create_datagram_endpoint's
        # contract instead of a runtime isinstance check.
        transport = cast(asyncio.DatagramTransport, created_transport)
        assert isinstance(protocol, _QueryDatagramProtocol)

        transport.sendto(QUERY_MAGIC + bytes((HANDSHAKE_TYPE,)) + session_id)
        challenge = _parse_challenge(await protocol.receive(), session_id)

        full_stat_request = (
            QUERY_MAGIC
            + bytes((STAT_TYPE,))
            + session_id
            + struct.pack(">i", challenge)
            + FULL_STAT_PADDING
        )
        transport.sendto(full_stat_request)
        return _parse_players(await protocol.receive(), session_id)
    finally:
        if transport is not None:
            transport.close()


async def query_players(
    host: str,
    port: int,
    timeout_seconds: float,
) -> tuple[str, ...]:
    """Return the unique player names from a Minecraft Query full-stat reply.

    The timeout covers DNS resolution, the challenge handshake, and the full-stat
    request together. Player names are deduplicated case-insensitively while the
    first spelling returned by the server is preserved.
    """

    if not host:
        raise ValueError("host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    # Minecraft Query masks each session-ID byte to its low nibble. Generate an
    # already-masked value so Paper echoes exactly what the parser expects.
    session_id = bytes(value & 0x0F for value in secrets.token_bytes(4))
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _exchange(host, port, session_id)
    except TimeoutError as exc:
        raise MinecraftQueryTimeout(
            f"Minecraft Query to {host}:{port} timed out after {timeout_seconds:g} seconds"
        ) from exc
    except MinecraftQueryError:
        raise
    except OSError as exc:
        raise MinecraftQueryError(
            f"Minecraft Query to {host}:{port} failed: {exc}"
        ) from exc


__all__ = [
    "MinecraftQueryError",
    "MinecraftQueryProtocolError",
    "MinecraftQueryTimeout",
    "query_players",
]

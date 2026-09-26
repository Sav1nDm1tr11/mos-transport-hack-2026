from __future__ import annotations

import asyncio
import struct
from datetime import UTC, datetime

import pytest
from telemetry_gateway.protocol import Frame, ProtocolError, decode_frame
from telemetry_gateway.server import close_server, start_server


def modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def make_frame(
    *,
    unit_id: int = 1166336,
    request_id: int = 7,
    service_id: int = 1,
    message_type: int = 101,
    nph_flags: int = 1,
    npl_flags: int = 0,
    body: bytes | None = None,
) -> bytes:
    if body is None:
        # Independently specified 26-byte Nav00: 2024-01-01, lon 37.6173210 E,
        # lat 55.7551234 N, valid, avg speed 42, max 50, course 270.
        body = struct.pack(
            "<BBIIIBBHHHHHBB",
            0,
            0,
            1704067200,
            376173210,
            557551234,
            0xE0,
            12,
            42,
            50,
            270,
            123,
            156,
            8,
            4,
        )
    nph = struct.pack("<HHHI", service_id, message_type, nph_flags, request_id)
    crc = modbus_crc(nph + body)
    npl = struct.pack("<HHH", 0x7E7E, len(nph) + len(body), npl_flags)
    # The CRC bytes are stored high byte first, per the NPL byte-swap rule.
    npl += bytes((crc >> 8, crc & 0xFF)) + struct.pack("<BIH", 2, unit_id, 0)
    return npl + nph + body


def make_handshake(unit_id: int, request_id: int = 1) -> bytes:
    body = struct.pack("<HHHI II", 6, 2, 0, unit_id, 65535, 0)
    return make_frame(
        unit_id=unit_id,
        request_id=request_id,
        service_id=0,
        message_type=100,
        body=body,
    )


def test_decodes_golden_realtime_frame_and_navigation_signs() -> None:
    frame = decode_frame(make_frame())

    assert frame == Frame(
        unit_id=1166336,
        request_id=7,
        kind="realtime",
        payload=make_frame()[25:],
        navigation={
            "event_time": datetime(2024, 1, 1, tzinfo=UTC),
            "lat": 55.7551234,
            "lon": 37.617321,
            "speed_kmh": 42.0,
            "heading_deg": 270.0,
            "location_valid": True,
            "source_flags": 0xE0,
        },
    )


@pytest.mark.parametrize(
    ("flags", "lat", "lon", "valid"),
    [
        (0x00, None, None, False),
        (0xA0, 55.7551234, -37.617321, True),
        (0xC0, -55.7551234, 37.617321, True),
    ],
)
def test_navigation_flags_set_coordinate_signs_and_validity(
    flags: int, lat: float | None, lon: float | None, valid: bool
) -> None:
    body = struct.pack(
        "<BBIIIBBHHHHHBB",
        0,
        0,
        1704067200,
        376173210,
        557551234,
        flags,
        12,
        42,
        50,
        270,
        123,
        156,
        8,
        4,
    )
    nav = decode_frame(make_frame(body=body)).navigation
    assert nav is not None
    assert (nav["lat"], nav["lon"], nav["location_valid"]) == (lat, lon, valid)
    assert nav["source_flags"] == flags


def test_zero_coordinates_are_normalized_to_null_and_invalid() -> None:
    body = struct.pack(
        "<BBIIIBBHHHHHBB", 0, 0, 1704067200, 0, 0, 0xE0, 12, 0, 0, 0, 0, 0, 0, 0
    )
    nav = decode_frame(make_frame(body=body)).navigation
    assert nav is not None
    assert nav["lat"] is None and nav["lon"] is None
    assert nav["location_valid"] is False
    assert nav["source_flags"] == 0xE0


def test_invalid_flag_and_out_of_range_position_are_normalized_to_null() -> None:
    invalid_flag_body = struct.pack(
        "<BBIIIBBHHHHHBB",
        0,
        0,
        1704067200,
        376173210,
        557551234,
        0x60,
        12,
        42,
        50,
        270,
        123,
        156,
        8,
        4,
    )
    out_of_range_body = struct.pack(
        "<BBIIIBBHHHHHBB",
        0,
        0,
        1704067200,
        376173210,
        910000000,
        0xE0,
        12,
        42,
        50,
        270,
        123,
        156,
        8,
        4,
    )
    for body in (invalid_flag_body, out_of_range_body):
        nav = decode_frame(make_frame(body=body)).navigation
        assert nav is not None
        assert nav["lat"] is None and nav["lon"] is None
        assert nav["location_valid"] is False


def test_accepts_known_auxiliary_cell_after_navigation() -> None:
    body = make_frame()[25:] + bytes((8, 0)) + bytes(6)
    frame = decode_frame(make_frame(body=body))
    assert frame.navigation is not None
    assert frame.navigation["lat"] == 55.7551234


def test_unknown_tail_after_navigation_remains_opaque() -> None:
    tail = bytes((250, 0)) + b"opaque bytes whose length is not defined"
    frame = decode_frame(make_frame(body=make_frame()[25:] + tail))
    assert frame.navigation is not None
    assert frame.payload.endswith(tail)


@pytest.mark.parametrize(
    "mutation", ["signature", "length", "crc", "crc_order", "flags", "type"]
)
def test_rejects_invalid_npl_header(mutation: str) -> None:
    raw = bytearray(make_frame())
    if mutation == "signature":
        raw[0] ^= 1
    elif mutation == "length":
        raw[2] -= 1
    elif mutation == "crc":
        raw[6] ^= 1
    elif mutation == "crc_order":
        raw[6], raw[7] = raw[7], raw[6]
    elif mutation == "flags":
        raw[4] = 1
    elif mutation == "type":
        raw[8] = 3
    with pytest.raises(ProtocolError):
        decode_frame(bytes(raw))


def test_rejects_realtime_without_one_first_navigation_cell() -> None:
    with pytest.raises(ProtocolError):
        decode_frame(make_frame(body=bytes((8, 0)) + bytes(6)))
    with pytest.raises(ProtocolError):
        decode_frame(make_frame(body=make_frame()[25:] + make_frame()[25:]))


def test_rejects_truncated_or_malformed_navigation_payload() -> None:
    with pytest.raises(ProtocolError):
        decode_frame(make_frame()[:-1])
    with pytest.raises(ProtocolError):
        decode_frame(make_frame(body=bytes((0, 0)) + bytes(25)))


@pytest.mark.asyncio
async def test_server_handles_every_fragment_boundary_and_coalesced_frames() -> None:
    received: list[Frame] = []
    ready = asyncio.Event()

    async def callback(frame: Frame) -> None:
        received.append(frame)
        if len(received) == 3:
            ready.set()

    server = await start_server(callback, port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        hs = make_handshake(1166336)
        for i in range(len(hs)):
            writer.write(hs[i : i + 1])
            await writer.drain()
        pair = make_frame() + make_frame(request_id=8)
        writer.write(pair)
        await writer.drain()
        await asyncio.wait_for(ready.wait(), 2)
        assert [frame.request_id for frame in received] == [1, 7, 8]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_server_rejects_realtime_before_handshake_and_identity_mismatch() -> None:
    errors: list[tuple[str, int | None]] = []
    received: list[Frame] = []

    async def callback(frame: Frame) -> None:
        received.append(frame)

    async def on_error(reason: str, unit_id: int | None) -> None:
        errors.append((reason, unit_id))

    server = await start_server(callback, port=0, on_error=on_error)
    port = server.sockets[0].getsockname()[1]
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(make_frame())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(make_handshake(11) + make_frame(unit_id=12))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        for _ in range(20):
            if len(errors) >= 2:
                break
            await asyncio.sleep(0.01)
        assert [frame.kind for frame in received] == ["handshake"]
        assert len(errors) >= 2
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_server_callback_failure_does_not_stop_listener_or_reconnect() -> None:
    received: list[int] = []
    errors: list[str] = []

    async def callback(frame: Frame) -> None:
        if frame.request_id == 7:
            raise RuntimeError("storage unavailable")
        received.append(frame.request_id)

    async def on_error(reason: str, unit_id: int | None) -> None:
        errors.append(reason)

    server = await start_server(callback, port=0, on_error=on_error)
    port = server.sockets[0].getsockname()[1]
    try:
        for request_id in (7, 8):
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(make_handshake(1166336) + make_frame(request_id=request_id))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        for _ in range(20):
            if 8 in received:
                break
            await asyncio.sleep(0.01)
        assert received == [1, 1, 8]
        assert any("callback" in reason for reason in errors)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_close_server_stops_active_client_sessions() -> None:
    handshake_seen = asyncio.Event()

    async def callback(frame: Frame) -> None:
        if frame.kind == "handshake":
            handshake_seen.set()

    server = await start_server(callback, port=0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(make_handshake(1166336))
    await writer.drain()
    await asyncio.wait_for(handshake_seen.wait(), timeout=1)
    await asyncio.wait_for(close_server(server), timeout=1)
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_server_rejects_connections_above_configured_limit() -> None:
    received: list[Frame] = []
    handshake_seen = asyncio.Event()

    async def callback(frame: Frame) -> None:
        received.append(frame)
        if frame.kind == "handshake":
            handshake_seen.set()

    server = await start_server(callback, port=0, max_connections=1)
    port = server.sockets[0].getsockname()[1]
    first_writer: asyncio.StreamWriter | None = None
    try:
        _, first_writer = await asyncio.open_connection("127.0.0.1", port)
        first_writer.write(make_handshake(1166336))
        await first_writer.drain()
        await asyncio.wait_for(handshake_seen.wait(), timeout=1)
        _, rejected_writer = await asyncio.open_connection("127.0.0.1", port)
        rejected_writer.write(make_handshake(1166337) + make_frame(unit_id=1166337))
        await rejected_writer.drain()
        rejected_writer.close()
        await rejected_writer.wait_closed()
        await asyncio.sleep(0.02)
        assert [frame.kind for frame in received] == ["handshake"]
    finally:
        if first_writer is not None:
            first_writer.close()
            await first_writer.wait_closed()
        await close_server(server)

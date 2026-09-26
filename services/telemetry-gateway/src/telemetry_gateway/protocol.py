"""Decoder for the NDTP framing format used by the supplied emulator."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime

NPL_SIZE = 15
NPH_SIZE = 10
MAX_DATA_SIZE = 0xFFFF
NAV_PAYLOAD_SIZE = 26


class ProtocolError(ValueError):
    """A malformed or unsupported NDTP frame."""


@dataclass(frozen=True, slots=True)
class Frame:
    unit_id: int
    request_id: int
    kind: str
    payload: bytes
    navigation: dict | None


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def _navigation(body: bytes) -> dict:
    if len(body) < 2 + NAV_PAYLOAD_SIZE:
        raise ProtocolError("truncated navigation cell")
    cell_type, _number = struct.unpack_from("<BB", body)
    if cell_type != 0:
        raise ProtocolError("realtime frame must start with navigation cell")
    (
        timestamp,
        longitude,
        latitude,
        flags,
        _battery,
        speed_avg,
        _speed_max,
        course,
        _track,
        _altitude,
        _nsat,
        _pdop,
    ) = struct.unpack_from("<IIIBBHHHHHBB", body, 2)

    # Known auxiliary cells can be skipped by their specified fixed payload
    # lengths. An unknown cell makes the rest of the body opaque by design.
    known_payload_sizes = {2: 26, 8: 6, 10: 37, 15: 50, 16: 8}
    offset = 2 + NAV_PAYLOAD_SIZE
    seen_nav = 1
    while offset < len(body):
        if len(body) - offset < 2:
            raise ProtocolError("truncated cell header")
        next_type = body[offset]
        if next_type == 0:
            seen_nav += 1
            if seen_nav > 1:
                raise ProtocolError("duplicate navigation cell")
        size = known_payload_sizes.get(next_type)
        if size is None:
            break
        offset += 2 + size
        if offset > len(body):
            raise ProtocolError("truncated auxiliary cell")

    valid_flag = bool(flags & 0x80)
    lat = latitude / 10_000_000
    lon = longitude / 10_000_000
    if not flags & 0x20:
        lat = -lat
    if not flags & 0x40:
        lon = -lon
    if (
        not valid_flag
        or not latitude
        or not longitude
        or abs(lat) > 90
        or abs(lon) > 180
    ):
        valid_flag = False
        lat_value = lon_value = None
    else:
        lat_value, lon_value = lat, lon

    return {
        "event_time": datetime.fromtimestamp(timestamp, tz=UTC),
        "lat": lat_value,
        "lon": lon_value,
        "speed_kmh": float(speed_avg),
        "heading_deg": float(course),
        "location_valid": valid_flag,
        "source_flags": flags,
    }


def decode_frame(frame: bytes) -> Frame:
    """Validate and decode one complete NPL + NPH + body frame."""
    if len(frame) < NPL_SIZE + NPH_SIZE:
        raise ProtocolError("frame shorter than headers")
    signature, data_size, npl_flags = struct.unpack_from("<HHH", frame)
    if signature != 0x7E7E:
        raise ProtocolError("invalid NPL signature")
    if not NPH_SIZE <= data_size <= MAX_DATA_SIZE:
        raise ProtocolError("invalid NPL data size")
    if len(frame) != NPL_SIZE + data_size:
        raise ProtocolError("frame length does not match NPL data size")
    if npl_flags != 0:
        raise ProtocolError("unsupported NPL flags")
    expected_crc = _crc16_modbus(frame[NPL_SIZE:])
    wire_crc = (frame[6] << 8) | frame[7]
    if wire_crc != expected_crc:
        raise ProtocolError("invalid CRC")
    npl_type = frame[8]
    if npl_type != 2:
        raise ProtocolError("unsupported NPL type")
    unit_id = struct.unpack_from("<I", frame, 9)[0]
    service_id, message_type, nph_flags, request_id = struct.unpack_from(
        "<HHHI", frame, NPL_SIZE
    )
    if nph_flags != 1:
        raise ProtocolError("unsupported NPH flags")
    body = frame[NPL_SIZE + NPH_SIZE :]

    if (service_id, message_type) == (0, 100):
        if len(body) != 18:
            raise ProtocolError("invalid handshake payload size")
        (
            version_high,
            version_low,
            handshake_flags,
            handshake_unit,
            _max_size,
            reserved,
        ) = struct.unpack("<HHHIII", body)
        if (
            (version_high, version_low) != (6, 2)
            or handshake_flags != 0
            or reserved != 0
        ):
            raise ProtocolError("invalid handshake payload")
        if handshake_unit != unit_id:
            raise ProtocolError("handshake identity mismatch")
        return Frame(unit_id, request_id, "handshake", body, None)
    if (service_id, message_type) == (1, 101):
        navigation = _navigation(body)
        return Frame(unit_id, request_id, "realtime", body, navigation)
    raise ProtocolError("unsupported NPH service or message type")

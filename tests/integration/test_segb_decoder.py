from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime

from personal_data_platform.loader.models import RawObject
from personal_data_platform.loader.parser import parse_segb_bytes


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _field_varint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _field_bytes(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _field_double(field: int, value: float) -> bytes:
    return _varint((field << 3) | 1) + struct.pack("<d", value)


def _app_in_focus_payload() -> bytes:
    return b"".join(
        (
            _field_varint(2, 7),
            _field_varint(3, 1),
            _field_double(4, 10.5),
            _field_bytes(6, b"com.example.app"),
        )
    )


def _segb_v2_with_written_and_deleted(payload: bytes) -> bytes:
    entry = struct.pack("<Ii", zlib.crc32(payload), 0) + payload
    padding = b"\0" * (-len(entry) % 4)
    header = struct.pack("<4sid16s", b"SEGB", 2, 0.0, b"\0" * 16)
    trailers = b"".join(
        (
            struct.pack("<2id", len(entry), 1, 10.0),
            struct.pack("<2id", len(entry), 3, 11.0),
        )
    )
    return header + entry + padding + trailers


def test_pinned_ccl_segb_decodes_shared_offset_trailer_entries() -> None:
    segment = _segb_v2_with_written_and_deleted(_app_in_focus_payload())
    raw = RawObject(
        key="raw/screen_time/v1/device/app-in-focus/segment/time/hash.segb.gz",
        device_key="device",
        stream="app-in-focus",
        segment_key="segment",
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        sha256="a" * 64,
        storage_created_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        storage_generation=1,
    )

    records = parse_segb_bytes(raw, segment)

    assert len(records) == 2
    assert records[0].record_offset == records[1].record_offset
    assert records[0].record_metadata_offset != records[1].record_metadata_offset
    assert [record.record_state for record in records] == ["WRITTEN", "DELETED"]
    assert all(record.bundle_id == "com.example.app" for record in records)

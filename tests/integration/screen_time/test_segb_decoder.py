from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime

import pytest

from personal_data_platform.sources.screen_time.models import SegmentDecodeError
from personal_data_platform.sources.screen_time.parser import parse_segb_bytes
from personal_data_platform.sources.screen_time.raw import (
    ScreenTimeRawIdentity,
    parse_raw_object_key,
    sha256_hex,
)
from tests.screen_time_helpers import Repository, event, segb


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
    identity = ScreenTimeRawIdentity(
        device_key="b" * 64,
        stream="app-in-focus",
        segment_key="c" * 64,
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        sha256=sha256_hex(segment),
    )
    raw = parse_raw_object_key(
        identity.object_key,
        storage_created_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        storage_generation=1,
    )

    records = parse_segb_bytes(raw, segment)

    assert len(records) == 2
    assert records[0].record_offset == records[1].record_offset
    assert records[0].record_metadata_offset != records[1].record_metadata_offset
    assert [record.record_state for record in records] == ["WRITTEN", "DELETED"]
    assert all(record.bundle_id == "com.example.app" for record in records)


@pytest.mark.parametrize("state", [99, -1, 2, 0])
@pytest.mark.parametrize("mixed", [False, True])
def test_unknown_trailer_state_rejects_entire_segment(state, mixed):
    segment, offset = segb(event("app.valid"), state=state)
    if mixed:
        segment = bytearray(segment)
        struct.pack_into("<i", segment, 4, 2)
        segment += struct.pack("<2id", offset - 32, 1, 10.0)
        segment = bytes(segment)
    raw = Repository().add("100", segment)

    with pytest.raises(SegmentDecodeError, match="unsupported SEGB record state"):
        parse_segb_bytes(raw, segment)


@pytest.mark.parametrize("state", [99, -1, 2])
def test_unknown_state_is_rejected_even_without_a_data_offset(state):
    segment = struct.pack("<4sid16s2id", b"SEGB", 1, 0.0, b"\0" * 16, 0, state, 0.0)
    raw = Repository().add("100", segment)

    with pytest.raises(SegmentDecodeError, match="unsupported SEGB record state"):
        parse_segb_bytes(raw, segment)


@pytest.mark.parametrize("state", [0, 4])
@pytest.mark.parametrize("with_event", [False, True])
def test_known_empty_trailer_slots_remain_supported(state, with_event):
    if with_event:
        segment = bytearray(segb(event("app.valid"))[0])
        struct.pack_into("<i", segment, 4, 2)
    else:
        segment = bytearray(struct.pack("<4sid16s", b"SEGB", 1, 0.0, b"\0" * 16))
    segment += struct.pack("<2id", 0, state, 0.0)
    segment = bytes(segment)
    raw = Repository().add("100", segment)

    records = parse_segb_bytes(raw, segment)

    assert [record.bundle_id for record in records] == (["app.valid"] if with_event else [])


@pytest.mark.parametrize("count", [-1, 1, 3])
def test_invalid_trailer_extent_is_rejected(count):
    segment = struct.pack("<4sid16s", b"SEGB", count, 0.0, b"\0" * 16)
    raw = Repository().add("100", segment)

    with pytest.raises(SegmentDecodeError):
        parse_segb_bytes(raw, segment)


def test_empty_segment_without_trailer_entries_remains_supported():
    segment = struct.pack("<4sid16s", b"SEGB", 0, 0.0, b"\0" * 16)
    raw = Repository().add("100", segment)

    assert parse_segb_bytes(raw, segment) == []

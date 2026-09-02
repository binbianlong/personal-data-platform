from __future__ import annotations

import hashlib
import struct
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.loader.models import PayloadDecodeError, RawObject
from personal_data_platform.loader.parser import (
    CF_ABSOLUTE_TIME_EPOCH,
    decode_app_in_focus_payload,
    event_key,
    parse_segb_records,
)


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


def _payload(*, foreground: int = 1, timestamp: float = 10.5) -> bytes:
    return b"".join(
        (
            _field_bytes(1, b"foreground"),
            _field_varint(2, 7),
            _field_varint(3, foreground),
            _field_double(4, timestamp),
            _field_bytes(6, b"com.example.app"),
            _field_bytes(9, b"1.2.3"),
            _field_bytes(10, b"42"),
            _field_varint(13, 2),
            _field_varint(99, 1),
        )
    )


def _raw() -> RawObject:
    return RawObject(
        key="raw/screen_time/v1/device/App.InFocus/segment/2026-08-27T00:00:00Z/hash.segb.gz",
        device_key="device",
        stream="App.InFocus",
        segment_key="segment",
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        sha256="a" * 64,
        storage_created_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        storage_generation=1,
    )


def test_decode_app_in_focus_payload_preserves_observed_contract() -> None:
    decoded = decode_app_in_focus_payload(_payload())

    assert decoded["bundle_id"] == "com.example.app"
    assert decoded["in_foreground"] is True
    assert decoded["event_at"] == CF_ABSOLUTE_TIME_EPOCH + timedelta(seconds=10.5)
    assert decoded["cf_absolute_time"] == 10.5
    assert decoded["unknown_field_count"] == 1


def test_decode_rejects_non_boolean_foreground() -> None:
    with pytest.raises(PayloadDecodeError, match="field 3"):
        decode_app_in_focus_payload(_payload(foreground=2))


def test_event_identity_does_not_depend_on_segment_offset() -> None:
    first = SimpleNamespace(
        data=_payload(),
        data_start_offset=12,
        state=SimpleNamespace(name="WRITTEN"),
        timestamp1=datetime(2026, 8, 27, tzinfo=UTC),
        crc_passed=True,
    )
    second = SimpleNamespace(
        data=_payload(),
        data_start_offset=900,
        state=SimpleNamespace(name="WRITTEN"),
        timestamp1=datetime(2026, 8, 27, tzinfo=UTC),
        crc_passed=True,
    )

    left = parse_segb_records(_raw(), b"ignored", [first])[0]
    right = parse_segb_records(_raw(), b"ignored", [second])[0]

    assert left.event_key == right.event_key
    assert left.record_offset != right.record_offset


def test_event_key_uses_the_versioned_binary_contract() -> None:
    components = (b"device", b"app-in-focus", b"com.example.app")
    canonical = b"".join(
        (
            b"screen-time/event/v1\0",
            *(struct.pack(">I", len(value)) + value for value in components),
            struct.pack(">d", 10.5),
            struct.pack(">I", 1),
            struct.pack(">I", 7),
        )
    )

    assert (
        event_key(
            device_key="device",
            stream="app-in-focus",
            bundle_id="com.example.app",
            cf_absolute_time=10.5,
            in_foreground=True,
            kind=7,
        )
        == hashlib.sha256(canonical).hexdigest()
    )


def test_shared_v2_data_offset_keeps_distinct_trailer_occurrences() -> None:
    written = SimpleNamespace(
        data=_payload(),
        data_start_offset=12,
        metadata=SimpleNamespace(metadata_offset=900, creation=None),
        state=SimpleNamespace(name="Written"),
        crc_passed=True,
    )
    deleted = SimpleNamespace(
        data=_payload(),
        data_start_offset=12,
        metadata=SimpleNamespace(metadata_offset=916, creation=None),
        state=SimpleNamespace(name="Deleted"),
        crc_passed=True,
    )

    records = parse_segb_records(_raw(), b"ignored", [written, deleted])

    assert [record.record_offset for record in records] == [12, 12]
    assert [record.record_metadata_offset for record in records] == [900, 916]
    assert [record.record_state for record in records] == ["WRITTEN", "DELETED"]

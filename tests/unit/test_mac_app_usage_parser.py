from __future__ import annotations

import struct
from datetime import UTC, datetime

import pytest

from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.models import PayloadDecodeError
from personal_data_platform.sources.screen_time.parser import decode_mac_app_usage_payload
from personal_data_platform.sources.screen_time.raw import (
    ScreenTimeRawIdentity,
    encode_segment_envelope,
    parse_raw_object_key,
    sha256_hex,
)
from tests.screen_time_helpers import NOW, segb, text_field, tombstone, varint


def _payload(*, start: int = 1, timestamp: float = 1_789_000_000.5) -> bytes:
    return (
        b"\x08"
        + varint(start)
        + b"\x11"
        + struct.pack("<d", timestamp)
        + text_field(3, "com.example.mac")
        + b"\x28\x01"
    )


def _raw(payload: bytes):
    identity = ScreenTimeRawIdentity(
        device_key="b" * 64,
        stream="app-usage",
        segment_key="c" * 64,
        observed_at=NOW,
        sha256=sha256_hex(payload),
        schema_version=2,
    )
    return parse_raw_object_key(identity.object_key, storage_created_at=NOW, storage_generation=1)


def _decode(payload: bytes, *, kind: str = "events", state: int = 1, crc: int | None = None):
    segment, _ = segb(payload, state=state, crc=crc)
    envelope = encode_segment_envelope(segment, name="123456", kind=kind)
    return get_source("screen_time", "app-usage").decode(_raw(envelope), envelope)


def test_app_usage_events_use_unix_time_and_a_distinct_parser_version() -> None:
    start = _decode(_payload(start=1)).records[0]
    end = _decode(_payload(start=0)).records[0]

    assert start.in_foreground is True and end.in_foreground is False
    assert start.event_at == datetime.fromtimestamp(1_789_000_000.5, UTC)
    assert start.cf_absolute_time == 1_789_000_000.5 - 978_307_200
    assert start.bundle_id == "com.example.mac"
    assert start.kind is None and start.unknown_field_count == 1
    assert start.source_stream == "app-usage"
    assert start.parser_version == "app-usage-v1"
    assert start.event_key != end.event_key


@pytest.mark.parametrize(
    "payload",
    [
        _payload(start=2),
        b"\x08\x01" + text_field(3, "com.example.mac"),
        b"\x08\x01\x11" + struct.pack("<d", float("nan")) + text_field(3, "app"),
        b"\x08\x01\x11" + struct.pack("<d", 1_789_000_000.5) + text_field(3, ""),
    ],
)
def test_app_usage_rejects_invalid_required_fields(payload: bytes) -> None:
    with pytest.raises(PayloadDecodeError):
        decode_mac_app_usage_payload(payload)


def test_app_usage_preserves_deleted_crc_and_tombstone_records() -> None:
    deleted = _decode(_payload(), state=3).records[0]
    corrupt = _decode(_payload(), crc=0).records[0]
    erased = _decode(b"\0" * 8, state=3, crc=0).records[0]
    deletion = _decode(tombstone("123456", 32, 64), kind="tombstones").records[0]

    assert deleted.record_kind == "deleted" and deleted.event_key is None
    assert deleted.bundle_id == "com.example.mac"
    assert corrupt.record_kind == "crc_failure" and corrupt.event_key is None
    assert erased.record_kind == "deleted" and erased.bundle_id is None
    assert deletion.record_kind == "tombstone" and deletion.target_segment_name == "123456"
    assert {record.parser_version for record in (deleted, corrupt, erased, deletion)} == {
        "app-usage-v1"
    }


def test_empty_mac_segment_records_its_own_parser_version() -> None:
    # A header with zero entries is a complete SEGB v2 file.
    empty_segment = struct.pack("<4sid16s", b"SEGB", 0, 0.0, b"\0" * 16)
    envelope = encode_segment_envelope(empty_segment, name="123456", kind="events")
    batch = get_source("screen_time", "app-usage").decode(_raw(envelope), envelope)

    assert batch.record_count == 0
    assert batch.parser_version == "app-usage-v1"

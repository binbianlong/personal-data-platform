"""Synthetic Screen Time inputs shared by unit and integration tests."""

import gzip
import struct
import zlib
from datetime import UTC, datetime, timedelta

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.screen_time.models import ParsedScreenTimeRecord
from personal_data_platform.sources.screen_time.raw import (
    ScreenTimeRawIdentity,
    encode_segment_envelope,
    parse_raw_object_key,
    sha256_hex,
)

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def varint(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    return bytes(result) + bytes([n])


def text_field(tag, value):
    value = value.encode()
    return varint(tag * 8 + 2) + varint(len(value)) + value


def event(bundle, timestamp=10.0, *, foreground=True):
    return (
        b"\x10\x01\x18"
        + bytes([int(foreground)])
        + b"\x21"
        + struct.pack("<d", timestamp)
        + text_field(6, bundle)
    )


def mac_usage_event(bundle, timestamp, *, start):
    return (
        b"\x08"
        + varint(int(start))
        + b"\x11"
        + struct.pack("<d", timestamp)
        + text_field(3, bundle)
        + b"\x28\x01"
    )


def segb(payload, *, state=1, timestamp=10.0, crc=None):
    entry = struct.pack("<Ii", zlib.crc32(payload) if crc is None else crc, 0) + payload
    metadata_offset = 32 + len(entry) + (-len(entry) % 4)
    result = (
        struct.pack("<4sid16s", b"SEGB", 1, 0.0, b"\0" * 16)
        + entry
        + b"\0" * (-len(entry) % 4)
        + struct.pack("<2id", len(entry), state, timestamp)
    )
    return result, metadata_offset


def tombstone(name, offset, length, *, reason=2, timestamp=10.0):
    return (
        text_field(1, name)
        + b"\x10"
        + varint(offset)
        + b"\x18"
        + varint(length)
        + b"\x20"
        + varint(reason)
        + text_field(5, "synthetic")
        + b"\x31"
        + struct.pack("<d", timestamp)
    )


class Repository:
    def __init__(self):
        self.objects = {}

    def add(
        self,
        name,
        segment,
        *,
        kind="events",
        device="a" * 64,
        version=2,
        logical=None,
        stream="app-in-focus",
    ):
        value = encode_segment_envelope(segment, name=name, kind=kind) if version == 2 else segment
        identity = ScreenTimeRawIdentity(
            device_key=device,
            stream=stream,
            segment_key=logical or sha256_hex((kind + name).encode()),
            observed_at=NOW + timedelta(seconds=len(self.objects)),
            sha256=sha256_hex(value),
            schema_version=version,
        )
        raw = parse_raw_object_key(
            identity.object_key, storage_created_at=NOW, storage_generation=1
        )
        self.objects[raw.key] = raw, gzip.compress(value, mtime=0)
        return raw

    def list_raw(self, prefix):
        return [raw for raw, _ in self.objects.values() if raw.key.startswith(prefix)]

    def get_raw(self, key, *, generation):
        assert generation == 1
        return self.objects[key][1]


def _raw(
    *,
    storage_created_at: datetime = datetime(2026, 8, 27, 1, tzinfo=UTC),
    storage_generation: int = 1,
) -> RawObject:
    return RawObject(
        key="raw/screen_time/v1/device/App.InFocus/segment/2026-08-27T00:00:00Z/hash.segb.gz",
        source_id="screen_time",
        schema_version=1,
        subject_key="device",
        stream="App.InFocus",
        logical_key="segment",
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        sha256="a" * 64,
        storage_created_at=storage_created_at,
        storage_generation=storage_generation,
    )


def _record(raw: RawObject) -> ParsedScreenTimeRecord:
    return ParsedScreenTimeRecord(
        event_key="event",
        object_key=raw.key,
        device_key=raw.subject_key,
        source_stream=raw.stream,
        segment_key=raw.logical_key,
        segment_sha256=raw.sha256,
        observed_at=raw.observed_at,
        segment_filename="segment.segb",
        record_offset=12,
        record_metadata_offset=120,
        record_state="WRITTEN",
        segment_record_timestamp=raw.observed_at,
        crc_passed=True,
        transition_reason="foreground",
        kind=1,
        in_foreground=True,
        cf_absolute_time=1.0,
        event_at=datetime(2001, 1, 1, 0, 0, 1, tzinfo=UTC),
        bundle_id="com.example.app",
        app_version="1",
        app_build="1",
        platform_flag=2,
        unknown_field_count=0,
        original_payload=b"payload",
        parser_version="app-in-focus-v1",
    )

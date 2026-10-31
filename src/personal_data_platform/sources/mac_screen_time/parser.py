"""Bind the source payload decoder to neutral SEGB parsing."""

from collections.abc import Iterable

from personal_data_platform.biome.models import ParsedScreenTimeRecord
from personal_data_platform.biome.parser import (
    UNIX_TO_CF_SECONDS,
    DecodedScreenTimeEvent,
    SegbRecord,
)
from personal_data_platform.biome.parser import (
    parse_segb_bytes as _parse_bytes,
)
from personal_data_platform.biome.parser import (
    parse_segb_records as _parse_records,
)
from personal_data_platform.raw.models import RawObject

from .payloads import decode_mac_app_usage_payload

PARSER_VERSION = "app-usage-v1"


def decode_event(payload: bytes) -> DecodedScreenTimeEvent:
    decoded = decode_mac_app_usage_payload(payload)
    return DecodedScreenTimeEvent(
        in_foreground=decoded["in_foreground"],
        cf_absolute_time=decoded["unix_time"] - UNIX_TO_CF_SECONDS,
        event_at=decoded["event_at"],
        bundle_id=decoded["bundle_id"],
        unknown_field_count=decoded["unknown_field_count"],
    )


def parse_segb_bytes(
    raw: RawObject, segment: bytes, *, segment_kind: str = "events"
) -> list[ParsedScreenTimeRecord]:
    return _parse_bytes(
        raw,
        segment,
        segment_kind=segment_kind,
        decode_event=decode_event,
        parser_version=PARSER_VERSION,
    )


def parse_segb_records(
    raw: RawObject, segment: bytes, records: Iterable[SegbRecord], *, segment_kind: str = "events"
) -> list[ParsedScreenTimeRecord]:
    return _parse_records(
        raw,
        segment,
        records,
        segment_kind=segment_kind,
        decode_event=decode_event,
        parser_version=PARSER_VERSION,
    )

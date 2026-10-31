"""Bind the source payload decoder to neutral SEGB parsing."""

from collections.abc import Iterable

from personal_data_platform.biome.models import ParsedScreenTimeRecord
from personal_data_platform.biome.parser import (
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

from .payloads import decode_app_in_focus_payload

PARSER_VERSION = "app-in-focus-v2"


def decode_event(payload: bytes) -> DecodedScreenTimeEvent:
    return DecodedScreenTimeEvent(**decode_app_in_focus_payload(payload))


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

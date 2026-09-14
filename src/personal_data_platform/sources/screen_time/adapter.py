"""Screen Time App.InFocus ingestion and recovery contract."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import DecodedBatch, RawRepository, SourceHealth

from .parser import PARSER_VERSION, parse_segb_bytes
from .raw import (
    APP_IN_FOCUS_STREAM,
    RAW_PREFIX,
    RAW_V2_PREFIX,
    decode_segment_envelope,
    parse_raw_object_key,
    validate_raw_object_key,
)


class ScreenTimeSource:
    """Read legacy SEGB and v2 observations with source identities for deletion matching."""

    source_id = "screen_time"
    stream = APP_IN_FOCUS_STREAM
    schema_versions = (1, 2)
    parser_version = PARSER_VERSION
    raw_prefixes = (f"{RAW_PREFIX}/", f"{RAW_V2_PREFIX}/")
    raw_suffixes = (".segb.gz",)
    required_relations = (
        "base.screen_time_event",
        "base.screen_time_segment_observation",
        "base.screen_time_record_occurrence",
        "base.screen_time_transition",
        "base.screen_time_tombstone_match",
        "base.screen_time_tombstone_status",
        "base.screen_time_interval",
        "marts.daily_screen_time",
    )
    retention_days = 90
    lifecycle_grace_days = 3
    dbt_selector = "tag:screen_time_app_in_focus"
    monitor_name = "screen_time_reconciliation"

    def __init__(self, *, known_streams: tuple[str, ...] = (APP_IN_FOCUS_STREAM,)) -> None:
        self._known_streams = known_streams

    def validate_raw_key(self, key: str) -> None:
        validate_raw_object_key(key)
        # Listing a version prefix can include other registered streams. Their
        # identities can be parsed, while decode remains scoped to this stream.
        stream = key.split("/")[4]
        if stream not in self._known_streams:
            raise ValueError(f"unsupported Screen Time stream: {stream}")

    def parse_raw_key(
        self, key: str, *, storage_created_at: datetime, storage_generation: int
    ) -> RawObject:
        self.validate_raw_key(key)
        return parse_raw_object_key(
            key,
            storage_created_at=storage_created_at,
            storage_generation=storage_generation,
        )

    def decode(self, raw: RawObject, payload: bytes) -> DecodedBatch:
        from .writer import ScreenTimeBatch

        if (
            raw.source_id != self.source_id
            or raw.stream != self.stream
            or raw.schema_version not in self.schema_versions
        ):
            raise ValueError("Raw source, stream or schema version does not match Screen Time")
        self.validate_raw_key(raw.key)
        name = kind = None
        if raw.schema_version == 2:
            payload, name, kind = decode_segment_envelope(payload)
        records = parse_segb_bytes(raw, payload, segment_kind=kind)
        if name is not None:
            records = [replace(record, segment_filename=name) for record in records]
        return ScreenTimeBatch(
            records,
            source_segment_name=name,
            segment_kind=kind,
        )

    def repository_from_env(self) -> RawRepository:
        from .storage import ScreenTimeGCSRepository

        return ScreenTimeGCSRepository.from_env(source=self)

    def audit(
        self, repository: RawRepository, observations: Sequence[RawObject], now: datetime
    ) -> SourceHealth:
        from .audit import audit_source

        return audit_source(repository, observations, now)

    def legacy_scope(self, raw: RawObject) -> tuple[str, str]:
        return raw.subject_key, raw.logical_key

    def inventory(self, observations: Sequence[RawObject]) -> dict[str, object]:
        return {
            "device_count": len({raw.subject_key for raw in observations}),
            "segment_count": len(
                {(raw.subject_key, raw.stream, raw.logical_key) for raw in observations}
            ),
        }

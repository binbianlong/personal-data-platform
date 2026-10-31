"""Screen Time stream ingestion and recovery contracts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import DecodedBatch, RawRepository, SourceHealth

from .parser import PARSER_VERSION, parse_segb_bytes
from .raw import (
    APP_USAGE_STREAM,
    RAW_PREFIX,
    RAW_SCHEMA_VERSION,
    decode_segment_envelope,
    parse_raw_object_key,
    validate_raw_object_key,
)


class MacScreenTimeSource:
    """Read stored Screen Time observations into events."""

    source_id = "mac_screen_time"
    stream = APP_USAGE_STREAM
    raw_storage: Literal["gcs", "sqlite"] = "sqlite"
    schema_versions: tuple[int, ...] = (RAW_SCHEMA_VERSION,)
    parser_version = PARSER_VERSION
    raw_prefixes: tuple[str, ...] = (f"{RAW_PREFIX}/",)
    raw_suffixes: tuple[str, ...] = (".segb.gz",)
    required_relations: tuple[str, ...] = (
        "base.mac_screen_time_event",
        "base.mac_screen_time_transition",
        "base.mac_screen_time_interval",
        "marts.daily_mac_screen_time",
        "marts.daily_mac_screen_time_total",
    )
    dbt_selector = "tag:mac_screen_time"
    monitor_name = "mac_screen_time_reconciliation"

    def validate_raw_key(self, key: str) -> None:
        validate_raw_object_key(key)

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
        from .batch import MacScreenTimeBatch

        if (
            raw.source_id != self.source_id
            or raw.stream != self.stream
            or raw.schema_version not in self.schema_versions
        ):
            raise ValueError("Raw source, stream or schema version does not match Screen Time")
        self.validate_raw_key(raw.key)
        payload, kind = decode_segment_envelope(payload)
        records = [] if kind == "tombstones" else parse_segb_bytes(raw, payload, segment_kind=kind)
        return MacScreenTimeBatch(
            records,
            segment_kind=kind,
            parser_version_override=self.parser_version,
        )

    def repository_from_env(self) -> RawRepository:
        from .storage import LocalRepository

        return LocalRepository.from_env(source=self)

    def audit(
        self, repository: RawRepository, observations: Sequence[RawObject], now: datetime
    ) -> SourceHealth:
        from personal_data_platform.biome.audit import audit_source

        return audit_source(
            repository,
            observations,
            now,
            allow_inactive=True,
        )

    def inventory(self, observations: Sequence[RawObject]) -> dict[str, object]:
        return {
            "device_count": len({raw.subject_key for raw in observations}),
            "segment_count": len(
                {(raw.subject_key, raw.stream, raw.logical_key) for raw in observations}
            ),
        }

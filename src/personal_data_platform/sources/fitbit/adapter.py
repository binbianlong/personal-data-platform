"""Fitbit's contract with the source-independent Loader and reconciliation."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository, SourceHealth
from personal_data_platform.storage.gcs import GCSRawRepository

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from .models import PARSER_VERSION, TABLES, CapturedSnapshot, HeartRateMinuteSnapshot, Snapshot
from .raw import BUNDLE_PREFIX, PREFIX, decode_bundle, parse_bundle_key, parse_raw_key
from .writer import FitbitBatch, FitbitMinuteBatch


class FitbitSource:
    source_id = "fitbit"
    stream = "health"
    schema_versions: tuple[int, ...] = (1,)
    raw_prefixes: tuple[str, ...] = (PREFIX,)
    raw_suffixes: tuple[str, ...] = (".json.gz",)
    retention_days = 90
    lifecycle_grace_days = 3
    parser_version = PARSER_VERSION
    dbt_selector = "tag:fitbit tag:screen_time"
    monitor_name = "fitbit"
    required_relations = tuple(f"base.{name}" for name in TABLES.values()) + (
        "ops.fitbit_coverage",
        "ops.fitbit_deleted_record",
        "ops.fitbit_raw_intent",
        "marts.daily_fitbit_health",
        "marts.fitbit_steps_time_series",
        "marts.fitbit_heart_rate_time_series",
        "marts.fitbit_sleep_sessions",
        "marts.fitbit_sleep_screen_time",
    )

    def __init__(self, *, version: int | None = None) -> None:
        selected = (
            version
            if version is not None
            else (
                3
                if os.environ.get("PDP_FITBIT_DELIVERY_MODE") == "pubsub"
                or os.environ.get("PDP_SCHEMA_PROFILE") == "west"
                else 1
            )
        )
        if selected not in (1, 3):
            raise ValueError("unsupported Fitbit Raw version")
        if selected == 3:
            self.schema_versions = (3,)
            self.raw_prefixes = (BUNDLE_PREFIX,)
            self.parser_version = "fitbit-v3"
            self.required_relations = tuple(
                f"base.{name}" for kind, name in TABLES.items() if kind != "heart-rate"
            ) + (
                "base.fitbit_heart_rate_minute",
                "ops.fitbit_coverage",
                "ops.fitbit_minute_coverage",
                "ops.fitbit_deleted_record",
                "marts.daily_fitbit_health",
                "marts.fitbit_heart_rate_minute_time_series",
            )

    def validate_raw_key(self, key: str) -> None:
        self.parse_raw_key(key, storage_created_at=datetime.now(UTC), storage_generation=1)

    def parse_raw_key(
        self, key: str, *, storage_created_at: datetime, storage_generation: int
    ) -> RawObject:
        parser = parse_bundle_key if self.schema_versions == (3,) else parse_raw_key
        return parser(
            key, storage_created_at=storage_created_at, storage_generation=storage_generation
        )

    def decode(self, raw: RawObject, payload: bytes) -> FitbitBatch | BundleBatch:
        if self.schema_versions == (3,):
            entries = decode_bundle(payload)
            if (
                any(entry.subject_key != raw.subject_key for entry in entries)
                or max(entry.fetched_at for entry in entries) != raw.observed_at
            ):
                raise ValueError("Raw acquisition identity mismatch")
            return BundleBatch(entries)
        snapshot = Snapshot.from_bytes(payload)
        if (
            snapshot.subject_key,
            snapshot.window.data_type,
            snapshot.fetched_at,
            snapshot.origin,
        ) != (raw.subject_key, raw.logical_key, raw.observed_at, "api"):
            raise ValueError("Fitbit Raw envelope does not match its identity")
        return FitbitBatch(snapshot)

    def repository_from_env(self) -> RawRepository:
        return GCSRawRepository.from_env(source=self)

    def audit(
        self, repository: RawRepository, observations: Sequence[RawObject], now: datetime
    ) -> SourceHealth:
        # Acquisition silence alone does not prove delivery failure. Receipt backlog
        # health is reported separately by the repair worker.
        latest = max((raw.observed_at for raw in observations), default=None)
        return SourceHealth(
            True,
            {
                "latest_acquisition_at": latest.isoformat() if latest else None,
                "raw_objects": len(observations),
            },
        )

    def inventory(self, observations: Sequence[RawObject]) -> dict[str, object]:
        return {
            "raw_objects": len(observations),
            "subjects": sorted({raw.subject_key for raw in observations}),
        }


@dataclass(frozen=True, slots=True)
class BundleBatch:
    entries: tuple[CapturedSnapshot | HeartRateMinuteSnapshot, ...]
    parser_version: str = "fitbit-v3"

    @property
    def record_count(self) -> int:
        return sum(
            len(entry.minutes)
            if isinstance(entry, HeartRateMinuteSnapshot)
            else len(entry.snapshot.records)
            for entry in self.entries
        )

    def write(
        self, connection: DuckDBPyConnection, raw: RawObject, *, byte_size: int, loaded_at: datetime
    ) -> None:
        for entry in self.entries:
            if isinstance(entry, HeartRateMinuteSnapshot):
                FitbitMinuteBatch(entry).write_snapshot(
                    connection, source_key=raw.key, loaded_at=loaded_at
                )
            else:
                FitbitBatch(entry.snapshot, source_digest=entry.source_sha256()).write_snapshot(
                    connection, source_key=raw.key, loaded_at=loaded_at
                )

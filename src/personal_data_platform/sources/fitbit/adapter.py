"""Fitbit's contract with the source-independent Loader and reconciliation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository, SourceHealth
from personal_data_platform.storage.gcs import GCSRawRepository

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from .models import TABLES, CapturedSnapshot, HeartRateMinuteSnapshot
from .raw import BUNDLE_PREFIX, decode_bundle, parse_bundle_key
from .writer import FitbitBatch, FitbitMinuteBatch


class FitbitSource:
    source_id = "fitbit"
    stream = "health"
    schema_versions: tuple[int, ...] = (3,)
    raw_prefixes: tuple[str, ...] = (BUNDLE_PREFIX,)
    raw_suffixes: tuple[str, ...] = (".json.gz",)
    retention_days = 90
    lifecycle_grace_days = 3
    parser_version = "fitbit-v3"
    dbt_selector = "tag:fitbit tag:screen_time"
    monitor_name = "fitbit"
    required_relations = tuple(f"base.{name}" for name in dict.fromkeys(TABLES.values())) + (
        "base.fitbit_heart_rate_minute",
        "ops.fitbit_coverage",
        "ops.fitbit_deleted_record",
        "marts.daily_fitbit_health",
        "marts.fitbit_heart_rate_minute_time_series",
    )

    def __init__(self, *, version: int | None = None) -> None:
        if version is not None and version != 3:
            raise ValueError("unsupported Fitbit Raw version")

    def validate_raw_key(self, key: str) -> None:
        self.parse_raw_key(key, storage_created_at=datetime.now(UTC), storage_generation=1)

    def parse_raw_key(
        self, key: str, *, storage_created_at: datetime, storage_generation: int
    ) -> RawObject:
        return parse_bundle_key(
            key, storage_created_at=storage_created_at, storage_generation=storage_generation
        )

    def decode(self, raw: RawObject, payload: bytes) -> BundleBatch:
        entries = decode_bundle(payload)
        if (
            raw.schema_version != 3
            or any(entry.subject_key != raw.subject_key for entry in entries)
            or max(entry.fetched_at for entry in entries) != raw.observed_at
        ):
            raise ValueError("Raw acquisition identity mismatch")
        return BundleBatch(entries)

    def repository_from_env(self) -> RawRepository:
        return GCSRawRepository.from_env(source=self)

    def audit(
        self, repository: RawRepository, observations: Sequence[RawObject], now: datetime
    ) -> SourceHealth:
        # Acquisition silence alone does not prove delivery failure.
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

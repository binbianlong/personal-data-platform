"""Fitbit's contract with the source-independent Loader and reconciliation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository, SourceHealth
from personal_data_platform.storage.gcs import GCSRawRepository

from .models import PARSER_VERSION, TABLES, Snapshot
from .raw import BUNDLE_PREFIX, PREFIX, SnapshotBundle, parse_raw_key
from .writer import FitbitBatch, FitbitBundleBatch


class FitbitSource:
    source_id = "fitbit"
    stream = "health"
    schema_versions: tuple[int, ...] = (1, 2)
    raw_prefixes: tuple[str, ...] = (PREFIX, BUNDLE_PREFIX)
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
        "ops.fitbit_daily_state",
        "ops.fitbit_batch_intent",
        "marts.daily_fitbit_health",
        "marts.fitbit_steps_time_series",
        "marts.fitbit_heart_rate_time_series",
        "marts.fitbit_sleep_sessions",
        "marts.fitbit_sleep_screen_time",
    )

    def validate_raw_key(self, key: str) -> None:
        parse_raw_key(key, storage_created_at=datetime.now(UTC), storage_generation=1)

    def parse_raw_key(
        self, key: str, *, storage_created_at: datetime, storage_generation: int
    ) -> RawObject:
        return parse_raw_key(
            key, storage_created_at=storage_created_at, storage_generation=storage_generation
        )

    def decode(self, raw: RawObject, payload: bytes) -> FitbitBatch | FitbitBundleBatch:
        if raw.schema_version == 2:
            bundle = SnapshotBundle.from_bytes(payload)
            if (bundle.subject_key, bundle.fetched_at, raw.logical_key) != (
                raw.subject_key,
                raw.observed_at,
                "batch",
            ):
                raise ValueError("Fitbit Raw bundle does not match its identity")
            return FitbitBundleBatch(bundle.snapshots)
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

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
from personal_data_platform.storage.motherduck import Warehouse

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from .acquisition_state import AcquisitionState
from .models import PARSER_VERSION, TABLES, FitbitBundle, HeartRateMinuteSnapshot, Snapshot
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
                2
                if os.environ.get("PDP_FITBIT_DELIVERY_MODE") == "pubsub"
                or os.environ.get("PDP_SCHEMA_PROFILE") == "west"
                else 1
            )
        )
        if selected not in (1, 2):
            raise ValueError("unsupported Fitbit Raw version")
        if selected == 2:
            self.schema_versions = (2,)
            self.raw_prefixes = (BUNDLE_PREFIX,)
            self.parser_version = "fitbit-v2"
            self.required_relations = tuple(
                f"base.{name}" for kind, name in TABLES.items() if kind != "heart-rate"
            ) + (
                "base.fitbit_heart_rate_minute",
                "ops.fitbit_attempt",
                "ops.fitbit_bundle",
                "marts.daily_fitbit_health",
                "marts.fitbit_heart_rate_minute_time_series",
            )

    def validate_raw_key(self, key: str) -> None:
        self.parse_raw_key(key, storage_created_at=datetime.now(UTC), storage_generation=1)

    def parse_raw_key(
        self, key: str, *, storage_created_at: datetime, storage_generation: int
    ) -> RawObject:
        parser = parse_bundle_key if self.schema_versions == (2,) else parse_raw_key
        return parser(
            key, storage_created_at=storage_created_at, storage_generation=storage_generation
        )

    def decode(self, raw: RawObject, payload: bytes) -> FitbitBatch | BundleBatch:
        if self.schema_versions == (2,):
            return self.decode_bundle((raw,), (payload,))[0]
        snapshot = Snapshot.from_bytes(payload)
        if (
            snapshot.subject_key,
            snapshot.window.data_type,
            snapshot.fetched_at,
            snapshot.origin,
        ) != (raw.subject_key, raw.logical_key, raw.observed_at, "api"):
            raise ValueError("Fitbit Raw envelope does not match its identity")
        return FitbitBatch(snapshot)

    def decode_bundle(
        self, refs: tuple[RawObject, ...], payloads: tuple[bytes, ...]
    ) -> tuple[BundleBatch, ...]:
        bundle = decode_bundle(payloads)
        if any(
            raw.subject_key != bundle.entries[0].acquisition.subject_key
            or raw.logical_key.split(":")[0] != bundle.bundle_id
            for raw in refs
        ):
            raise ValueError("bundle Raw scope mismatch")
        keys = tuple(raw.key for raw in refs)
        return tuple(BundleBatch(bundle, keys, index == 0) for index in range(len(refs)))

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
    bundle: FitbitBundle
    raw_keys: tuple[str, ...]
    apply_data: bool = True
    parser_version: str = "fitbit-v2"

    @property
    def record_count(self) -> int:
        if not self.apply_data:
            return 0
        return sum(
            len(entry.acquisition.minutes)
            if isinstance(entry.acquisition, HeartRateMinuteSnapshot)
            else len(entry.acquisition.snapshot.records)
            for entry in self.bundle.entries
        )

    def write(
        self, connection: DuckDBPyConnection, raw: RawObject, *, byte_size: int, loaded_at: datetime
    ) -> None:
        if not self.apply_data:
            return
        state = AcquisitionState(Warehouse(connection))
        for entry in self.bundle.entries:
            acquisition = entry.acquisition
            state.restore_attempt(entry.attempt_id, entry.scope, started_at=acquisition.fetched_at)
            if isinstance(acquisition, HeartRateMinuteSnapshot):
                FitbitMinuteBatch(acquisition).write_snapshot(
                    connection, source_key=raw.key, loaded_at=loaded_at
                )
            else:
                FitbitBatch(acquisition.snapshot).write_snapshot(
                    connection, source_key=raw.key, loaded_at=loaded_at
                )
            state.finish_attempt(
                entry.attempt_id, source_sha256=acquisition.source_sha256(), raw_keys=self.raw_keys
            )

"""Transactional MotherDuck/DuckDB persistence for decoded raw observations."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from personal_data_platform.loader.models import ParsedScreenTimeRecord, RawObject

DEFAULT_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@dataclass(frozen=True, slots=True)
class WarehouseConfig:
    """Connection details with secrets kept outside serialized settings."""

    database: str
    token: str | None = None

    @classmethod
    def from_env(cls) -> WarehouseConfig:
        database = os.environ.get("MOTHERDUCK_DATABASE")
        if not database:
            raise ValueError("MOTHERDUCK_DATABASE is required")
        return cls(database=database, token=os.environ.get("MOTHERDUCK_TOKEN"))


@dataclass(frozen=True, slots=True)
class IngestionState:
    """Retention-relevant state for one object known to the warehouse."""

    object_key: str
    status: str
    storage_created_at: datetime | None
    storage_generation: int | None
    retention_expired_at: datetime | None


def connect(config: WarehouseConfig) -> Any:
    """Connect to MotherDuck, or to a local DuckDB file for tests."""

    try:
        import duckdb
    except ImportError as error:  # pragma: no cover - packaging failure
        raise RuntimeError("duckdb is required for warehouse access") from error

    if config.token is None and (
        config.database == ":memory:" or config.database.endswith((".duckdb", ".db"))
    ):
        return duckdb.connect(config.database)
    if not config.token:
        raise ValueError("MOTHERDUCK_TOKEN is required for a MotherDuck database")
    return duckdb.connect(f"md:{config.database}", config={"motherduck_token": config.token})


class Warehouse:
    """Small repository that makes a raw object's load status atomic."""

    def __init__(self, connection: Any):
        self.connection = connection

    def close(self) -> None:
        self.connection.close()

    def migrate(self, migrations: Path = DEFAULT_MIGRATIONS) -> None:
        self.connection.execute("CREATE SCHEMA IF NOT EXISTS ops")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ops.schema_migration (
                migration_id VARCHAR PRIMARY KEY,
                checksum VARCHAR NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        for path in sorted(migrations.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            existing = self.connection.execute(
                "SELECT checksum FROM ops.schema_migration WHERE migration_id = ?", [path.name]
            ).fetchone()
            if existing:
                if existing[0] != checksum:
                    raise RuntimeError(f"applied migration changed: {path.name}")
                continue
            self.connection.execute("BEGIN TRANSACTION")
            try:
                self.connection.execute(sql)
                self.connection.execute(
                    "INSERT INTO ops.schema_migration VALUES (?, ?, ?)",
                    [path.name, checksum, datetime.now(UTC)],
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def succeeded_keys(self) -> set[str]:
        rows = self.connection.execute(
            """
            SELECT object_key
            FROM ops.ingestion_metadata
            WHERE status = 'succeeded' AND retention_expired_at IS NULL
            """
        ).fetchall()
        return {row[0] for row in rows}

    def succeeded_keys_for(self, raw_objects: Iterable[Any]) -> set[str]:
        """Return active successes whose stored GCS generation matches the live object."""

        live_generations = {value.key: int(value.storage_generation) for value in raw_objects}
        if not live_generations:
            return set()
        rows = self.connection.execute(
            """
            SELECT object_key, storage_generation
            FROM ops.ingestion_metadata
            WHERE status = 'succeeded' AND retention_expired_at IS NULL
            """
        ).fetchall()
        return {
            str(object_key)
            for object_key, storage_generation in rows
            if storage_generation is not None
            and live_generations.get(str(object_key)) == int(storage_generation)
        }

    def ingestion_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, count(*)
            FROM ops.ingestion_metadata
            WHERE retention_expired_at IS NULL
            GROUP BY status
            """
        ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def active_ingestion_states(self) -> dict[str, IngestionState]:
        """Return metadata that has not already been accepted as lifecycle-expired."""

        rows = self.connection.execute(
            """
            SELECT object_key, status, storage_created_at, storage_generation,
                   retention_expired_at
            FROM ops.ingestion_metadata
            WHERE retention_expired_at IS NULL
            """
        ).fetchall()
        return {
            row[0]: IngestionState(
                object_key=row[0],
                status=row[1],
                storage_created_at=row[2],
                storage_generation=row[3],
                retention_expired_at=row[4],
            )
            for row in rows
        }

    def retention_inventory_counts(self) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT
                count(*),
                count(*) FILTER (WHERE retention_expired_at IS NOT NULL)
            FROM ops.ingestion_metadata
            """
        ).fetchone()
        return {
            "total_object_count": int(row[0]),
            "expired_object_count": int(row[1]),
        }

    def mark_retention_expired(
        self, states: Iterable[IngestionState], *, expired_at: datetime
    ) -> set[str]:
        """Expire only the exact storage incarnation audited as absent."""

        expired: set[str] = set()
        for state in sorted(states, key=lambda value: value.object_key):
            row = self.connection.execute(
                """
            UPDATE ops.ingestion_metadata
            SET retention_expired_at = ?
            WHERE object_key = ?
              AND status = 'succeeded'
              AND retention_expired_at IS NULL
              AND storage_created_at IS NOT DISTINCT FROM ?
              AND storage_generation IS NOT DISTINCT FROM ?
            RETURNING object_key
                """,
                [
                    expired_at,
                    state.object_key,
                    state.storage_created_at,
                    state.storage_generation,
                ],
            ).fetchone()
            if row is not None:
                expired.add(str(row[0]))
        return expired

    def load_object(
        self,
        raw: RawObject,
        *,
        byte_size: int,
        records: Iterable[ParsedScreenTimeRecord],
    ) -> int:
        materialized = list(records)
        now = datetime.now(UTC)
        self.connection.execute("BEGIN TRANSACTION")
        try:
            current = self.connection.execute(
                """
                SELECT status, content_sha256, retention_expired_at, storage_generation
                FROM ops.ingestion_metadata
                WHERE object_key = ?
                """,
                [raw.key],
            ).fetchone()
            if current and current[1] != raw.sha256:
                raise RuntimeError(f"immutable object identity changed: {raw.key}")
            if (
                current
                and current[0] == "succeeded"
                and current[2] is None
                and current[3] == raw.storage_generation
            ):
                self.connection.execute("ROLLBACK")
                return 0

            self.connection.execute(
                """
                INSERT INTO ops.ingestion_metadata (
                    object_key, device_key, source_stream, segment_key, observed_at,
                    content_sha256, byte_size, status, parser_version, record_count,
                    started_at, completed_at, error_type, error_message, retry_count,
                    storage_created_at, storage_generation, retention_expired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'loading', ?, NULL, ?, NULL, NULL, NULL, 0, ?, ?, NULL)
                ON CONFLICT (object_key) DO UPDATE SET
                    status = 'loading', parser_version = excluded.parser_version,
                    started_at = excluded.started_at, completed_at = NULL,
                    error_type = NULL, error_message = NULL,
                    storage_created_at = excluded.storage_created_at,
                    storage_generation = excluded.storage_generation,
                    retention_expired_at = NULL,
                    retry_count = ops.ingestion_metadata.retry_count + 1
                """,
                [
                    raw.key,
                    raw.device_key,
                    raw.stream,
                    raw.segment_key,
                    raw.observed_at,
                    raw.sha256,
                    byte_size,
                    materialized[0].parser_version if materialized else "app-in-focus-v1",
                    now,
                    raw.storage_created_at,
                    raw.storage_generation,
                ],
            )
            self.connection.execute(
                "DELETE FROM base.screen_time_record_occurrence WHERE object_key = ?", [raw.key]
            )
            if materialized:
                self.connection.executemany(
                    """
                    INSERT INTO base.screen_time_record_occurrence VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [self._record_row(record, now) for record in materialized],
                )
            self.connection.execute(
                """
                INSERT INTO base.screen_time_segment_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (object_key) DO UPDATE SET
                    record_count = excluded.record_count,
                    parser_version = excluded.parser_version,
                    loaded_at = excluded.loaded_at
                """,
                [
                    raw.key,
                    raw.device_key,
                    raw.stream,
                    raw.segment_key,
                    raw.observed_at,
                    raw.sha256,
                    byte_size,
                    len(materialized),
                    materialized[0].parser_version if materialized else "app-in-focus-v1",
                    now,
                ],
            )
            self.connection.execute(
                """
                UPDATE ops.ingestion_metadata
                SET status = 'succeeded', record_count = ?, completed_at = ?
                WHERE object_key = ?
                """,
                [len(materialized), now, raw.key],
            )
            self.connection.execute("COMMIT")
            return len(materialized)
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _record_row(record: ParsedScreenTimeRecord, loaded_at: datetime) -> list[Any]:
        return [
            record.object_key,
            record.record_offset,
            record.record_metadata_offset,
            record.event_key,
            record.device_key,
            record.source_stream,
            record.segment_key,
            record.segment_sha256,
            record.observed_at,
            record.segment_filename,
            record.record_state,
            record.segment_record_timestamp,
            record.crc_passed,
            record.transition_reason,
            record.kind,
            record.in_foreground,
            record.cf_absolute_time,
            record.event_at,
            record.bundle_id,
            record.app_version,
            record.app_build,
            record.platform_flag,
            record.unknown_field_count,
            record.original_payload,
            record.parser_version,
            loaded_at,
        ]

    def mark_failed(self, raw: RawObject, *, byte_size: int, error: Exception) -> None:
        now = datetime.now(UTC)
        self.connection.execute(
            """
            INSERT INTO ops.ingestion_metadata (
                object_key, device_key, source_stream, segment_key, observed_at,
                content_sha256, byte_size, status, parser_version, record_count,
                started_at, completed_at, error_type, error_message, retry_count,
                storage_created_at, storage_generation, retention_expired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', NULL, NULL, ?, ?, ?, ?, 0, ?, ?, NULL)
            ON CONFLICT (object_key) DO UPDATE SET
                status = 'failed', completed_at = excluded.completed_at,
                error_type = excluded.error_type, error_message = excluded.error_message,
                storage_created_at = excluded.storage_created_at,
                storage_generation = excluded.storage_generation,
                retention_expired_at = NULL,
                retry_count = ops.ingestion_metadata.retry_count + 1
            """,
            [
                raw.key,
                raw.device_key,
                raw.stream,
                raw.segment_key,
                raw.observed_at,
                raw.sha256,
                byte_size,
                now,
                now,
                type(error).__name__,
                str(error)[:4000],
                raw.storage_created_at,
                raw.storage_generation,
            ],
        )

    def begin_job(self, job_name: str, run_id: str) -> None:
        self.connection.execute(
            "INSERT INTO ops.job_run VALUES (?, ?, 'running', ?, NULL, NULL)",
            [run_id, job_name, datetime.now(UTC)],
        )

    def acquire_job_lock(self, job_name: str, owner_id: str, *, lease_seconds: int) -> bool:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=lease_seconds)
        self.connection.execute("BEGIN TRANSACTION")
        try:
            current = self.connection.execute(
                "SELECT owner_id, expires_at FROM ops.job_lock WHERE job_name = ?", [job_name]
            ).fetchone()
            if current is not None and current[0] != owner_id and current[1] > now:
                self.connection.execute("ROLLBACK")
                return False
            if current is None:
                self.connection.execute(
                    "INSERT INTO ops.job_lock VALUES (?, ?, ?)",
                    [job_name, owner_id, expires_at],
                )
            else:
                self.connection.execute(
                    "UPDATE ops.job_lock SET owner_id = ?, expires_at = ? WHERE job_name = ?",
                    [owner_id, expires_at, job_name],
                )
            self.connection.execute("COMMIT")
            return True
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def release_job_lock(self, job_name: str, owner_id: str) -> None:
        self.connection.execute(
            "DELETE FROM ops.job_lock WHERE job_name = ? AND owner_id = ?", [job_name, owner_id]
        )

    def finish_job(self, run_id: str, *, succeeded: bool, details: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE ops.job_run SET status = ?, completed_at = ?, details = ?
            WHERE run_id = ?
            """,
            [
                "succeeded" if succeeded else "failed",
                datetime.now(UTC),
                json.dumps(details, sort_keys=True),
                run_id,
            ],
        )

    def query_value(self, sql: str, parameters: list[Any] | None = None) -> Any:
        row = self.connection.execute(sql, parameters or []).fetchone()
        return None if row is None else row[0]

    def query_rows(self, sql: str, parameters: list[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.connection.execute(sql, parameters or []).fetchall()

    def record_reconciliation(self, values: dict[str, Any]) -> None:
        row = asdict(values) if hasattr(values, "__dataclass_fields__") else values
        self.connection.execute(
            """
            INSERT INTO ops.reconciliation_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                status = excluded.status,
                completed_at = excluded.completed_at,
                raw_object_count = excluded.raw_object_count,
                loaded_object_count = excluded.loaded_object_count,
                missing_object_count = excluded.missing_object_count,
                failed_object_count = excluded.failed_object_count,
                details = excluded.details
            """,
            [
                row["run_id"],
                row["status"],
                row["started_at"],
                row["completed_at"],
                row["raw_object_count"],
                row["loaded_object_count"],
                row["missing_object_count"],
                row["failed_object_count"],
                json.dumps(row.get("details", {}), sort_keys=True),
            ],
        )

    def publish_heartbeat(self, monitor_name: str, run_id: str, details: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO ops.heartbeat VALUES (?, ?, ?, ?)
            ON CONFLICT (monitor_name) DO UPDATE SET
                succeeded_at = excluded.succeeded_at,
                run_id = excluded.run_id,
                details = excluded.details
            """,
            [monitor_name, datetime.now(UTC), run_id, json.dumps(details, sort_keys=True)],
        )

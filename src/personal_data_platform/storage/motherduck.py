"""Transactional MotherDuck/DuckDB persistence for decoded raw observations."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from personal_data_platform.network_diagnostics import instrument_connection, network_phase
from personal_data_platform.raw.models import RawObject
from personal_data_platform.reconciliation.models import DAILY_JOB_HISTORY_NAMES
from personal_data_platform.sources.contracts import DecodedBatch
from personal_data_platform.storage.migrations import DEFAULT_MIGRATIONS as DEFAULT_MIGRATIONS
from personal_data_platform.storage.migrations import apply_migrations

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


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
    """Current load status for one object known to the warehouse."""

    object_key: str
    status: str


def connect(config: WarehouseConfig) -> DuckDBPyConnection:
    """Connect to MotherDuck, or to a local DuckDB file for tests."""

    try:
        import duckdb
    except ImportError as error:  # pragma: no cover - packaging failure
        raise RuntimeError("duckdb is required for warehouse access") from error

    if config.token is None and (
        config.database == ":memory:" or config.database.endswith((".duckdb", ".db"))
    ):
        with network_phase("db_connect"):
            return duckdb.connect(config.database)
    if not config.token:
        raise ValueError("MOTHERDUCK_TOKEN is required for a MotherDuck database")
    with network_phase("db_connect"):
        return duckdb.connect(
            f"md:{config.database}",
            config={"motherduck_token": config.token, "motherduck_attach_mode": "single"},
        )


def _raw_identity(raw: RawObject) -> tuple[str, int, str, str, str, datetime, str]:
    return (
        raw.source_id,
        raw.schema_version,
        raw.subject_key,
        raw.stream,
        raw.logical_key,
        raw.observed_at,
        raw.sha256,
    )


class WarehouseConnectionError(RuntimeError):
    """The connection must be reopened before deciding whether a write committed."""


class Warehouse:
    """Small repository that makes a raw object's load status atomic."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self.connection = cast("DuckDBPyConnection", instrument_connection(connection))
        self.connection_usable = True

    def close(self) -> None:
        self.connection.close()

    def migrate(self, migrations: Path | None = None) -> None:
        apply_migrations(self, migrations)

    def succeeded_keys(self, *, source_id: str, stream: str) -> set[str]:
        rows = self.connection.execute(
            """
            SELECT object_key
            FROM ops.ingestion_metadata
            WHERE source_id = ? AND source_stream = ?
              AND status = 'succeeded'
            """,
            [source_id, stream],
        ).fetchall()
        return {row[0] for row in rows}

    def succeeded_keys_for(
        self, raw_objects: Iterable[RawObject], *, parser_version: str | None = None
    ) -> set[str]:
        """Return active successes matching the live Raw identity and GCS generation."""

        materialized = tuple(raw_objects)
        live_identities = {
            value.key: (*_raw_identity(value), value.storage_generation) for value in materialized
        }
        if not live_identities:
            return set()
        rows: list[tuple[Any, ...]] = []
        for source_id, stream in sorted(
            {(value.source_id, value.stream) for value in materialized}
        ):
            rows.extend(
                self.connection.execute(
                    """
                    SELECT object_key, source_id, schema_version, subject_key,
                           source_stream, logical_key, observed_at,
                           content_sha256, storage_generation
                    FROM ops.ingestion_metadata
                    WHERE source_id = ? AND source_stream = ?
                      AND status = 'succeeded'
                      AND (? IS NULL OR parser_version = ?)
                    """,
                    [source_id, stream, parser_version, parser_version],
                ).fetchall()
            )
        return {
            str(object_key)
            for object_key, *identity in rows
            if live_identities.get(str(object_key)) == tuple(identity)
        }

    def prune_screen_time_receipts(
        self, live_raw: Iterable[RawObject], *, parser_version: str
    ) -> None:
        """Remove superseded successes only where one live Raw is warehouse-confirmed."""

        by_scope: dict[tuple[str, str, str], list[RawObject]] = {}
        for raw in live_raw:
            if raw.source_id == "screen_time":
                by_scope.setdefault((raw.subject_key, raw.stream, raw.logical_key), []).append(raw)
        candidates = [values[0] for values in by_scope.values() if len(values) == 1]
        confirmed = self.succeeded_keys_for(candidates, parser_version=parser_version)
        if not confirmed:
            return
        try:
            self.connection.execute("BEGIN TRANSACTION")
        except Exception as error:
            self.connection_usable = False
            raise WarehouseConnectionError(
                "cannot begin receipt pruning; reopen warehouse"
            ) from error
        try:
            for raw in candidates:
                if raw.key not in confirmed:
                    continue
                self.connection.execute(
                    "DELETE FROM ops.ingestion_metadata "
                    "WHERE source_id = 'screen_time' AND status = 'succeeded' "
                    "AND subject_key = ? AND source_stream = ? AND logical_key = ? "
                    "AND (observed_at, object_key) < (?, ?)",
                    [raw.subject_key, raw.stream, raw.logical_key, raw.observed_at, raw.key],
                )
        except Exception as error:
            try:
                self.connection.execute("ROLLBACK")
            except Exception:
                self.connection_usable = False
                raise WarehouseConnectionError("receipt pruning rollback failed") from error
            raise
        try:
            self.connection.execute("COMMIT")
        except Exception as error:
            self.connection_usable = False
            raise WarehouseConnectionError("receipt pruning commit outcome unknown") from error

    def ingestion_counts(self, *, source_id: str, stream: str) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, count(*)
            FROM ops.ingestion_metadata
            WHERE source_id = ? AND source_stream = ?
            GROUP BY status
            """,
            [source_id, stream],
        ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def active_ingestion_states(self, *, source_id: str, stream: str) -> dict[str, IngestionState]:
        """Return the confirmed outcomes for a source stream."""
        rows = self.connection.execute(
            """SELECT object_key, status FROM ops.ingestion_metadata
            WHERE source_id = ? AND source_stream = ?""",
            [source_id, stream],
        ).fetchall()
        return {key: IngestionState(key, status) for key, status in rows}

    def _existing_object(self, raw: RawObject) -> tuple[Any, ...] | None:
        current = self.connection.execute(
            """
            SELECT status, content_sha256, storage_generation,
                   source_id, schema_version, subject_key, source_stream,
                   logical_key, observed_at, parser_version
            FROM ops.ingestion_metadata
            WHERE object_key = ?
            """,
            [raw.key],
        ).fetchone()
        if current and (*current[3:9], current[1]) != _raw_identity(raw):
            raise RuntimeError(f"immutable object identity changed: {raw.key}")
        return current

    def load_object(
        self,
        raw: RawObject,
        *,
        byte_size: int,
        batch: DecodedBatch,
        _in_transaction: bool = False,
    ) -> int:
        """Commit source records and their success state in one transaction."""

        if not self.connection_usable:
            raise WarehouseConnectionError("reopen warehouse after an uncertain transaction")
        now = datetime.now(UTC)
        try:
            if not _in_transaction:
                self.connection.execute("BEGIN TRANSACTION")
        except Exception as error:
            self.connection_usable = False
            raise WarehouseConnectionError("cannot begin transaction; reopen warehouse") from error
        try:
            current = self._existing_object(raw)
            if (
                current
                and current[0] == "succeeded"
                and current[2] == raw.storage_generation
                and current[9] == batch.parser_version
            ):
                if not _in_transaction:
                    self.connection.execute("ROLLBACK")
                return 0

            batch.write(self.connection, raw, byte_size=byte_size, loaded_at=now)
            self.connection.execute(
                """
                INSERT INTO ops.ingestion_metadata (
                    object_key, source_id, schema_version, subject_key, source_stream,
                    logical_key, observed_at, content_sha256, byte_size, status,
                    parser_version, record_count, started_at, completed_at, error_type,
                    error_message, retry_count, storage_created_at, storage_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, ?, ?,
                          NULL, NULL, 0, ?, ?)
                ON CONFLICT (object_key) DO UPDATE SET
                    status = 'succeeded', parser_version = excluded.parser_version,
                    subject_key = excluded.subject_key, logical_key = excluded.logical_key,
                    byte_size = excluded.byte_size, record_count = excluded.record_count,
                    started_at = excluded.started_at, completed_at = excluded.completed_at,
                    error_type = NULL, error_message = NULL,
                    storage_created_at = excluded.storage_created_at,
                    storage_generation = excluded.storage_generation,
                    retry_count = ops.ingestion_metadata.retry_count + 1
                """,
                [
                    raw.key,
                    raw.source_id,
                    raw.schema_version,
                    raw.subject_key,
                    raw.stream,
                    raw.logical_key,
                    raw.observed_at,
                    raw.sha256,
                    byte_size,
                    batch.parser_version,
                    batch.record_count,
                    now,
                    now,
                    raw.storage_created_at,
                    raw.storage_generation,
                ],
            )
        except Exception:
            if _in_transaction:
                raise
            try:
                self.connection.execute("ROLLBACK")
            except Exception as rollback_error:
                self.connection_usable = False
                raise WarehouseConnectionError(
                    "rollback failed; reopen warehouse"
                ) from rollback_error
            raise
        try:
            if not _in_transaction:
                self.connection.execute("COMMIT")
        except Exception as error:
            # Never write a failed receipt or process another Raw on an uncertain connection.
            # A new connection decides from the persisted success receipt.
            self.connection_usable = False
            raise WarehouseConnectionError("commit outcome unknown; reopen warehouse") from error
        return batch.record_count

    def load_objects(self, objects: Iterable[tuple[RawObject, int, DecodedBatch]]) -> int:
        """Commit a validated group of observations and source writes atomically."""
        if not self.connection_usable:
            raise WarehouseConnectionError("reopen warehouse after an uncertain transaction")
        self.connection.execute("BEGIN TRANSACTION")
        try:
            count = sum(
                self.load_object(raw, byte_size=size, batch=batch, _in_transaction=True)
                for raw, size, batch in objects
            )
        except Exception:
            try:
                self.connection.execute("ROLLBACK")
            except Exception as error:
                self.connection_usable = False
                raise WarehouseConnectionError("rollback failed; reopen warehouse") from error
            raise
        try:
            self.connection.execute("COMMIT")
        except Exception as error:
            self.connection_usable = False
            raise WarehouseConnectionError("commit outcome unknown; reopen warehouse") from error
        return count

    def mark_failed(
        self,
        raw: RawObject,
        *,
        byte_size: int,
        error: Exception,
    ) -> None:
        if not self.connection_usable:
            raise WarehouseConnectionError("reopen warehouse after an uncertain transaction")
        now = datetime.now(UTC)
        self.connection.execute("BEGIN TRANSACTION")
        try:
            self._existing_object(raw)
            self.connection.execute(
                """
                INSERT INTO ops.ingestion_metadata (
                    object_key, source_id, schema_version, subject_key, source_stream, logical_key,
                    observed_at, content_sha256, byte_size, status, parser_version, record_count,
                    started_at, completed_at, error_type, error_message, retry_count,
                    storage_created_at, storage_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'failed', NULL, NULL, ?, ?, ?, ?,
                          0, ?, ?)
                ON CONFLICT (object_key) DO UPDATE SET
                    status = 'failed', completed_at = excluded.completed_at,
                    subject_key = excluded.subject_key, logical_key = excluded.logical_key,
                    error_type = excluded.error_type, error_message = excluded.error_message,
                    storage_created_at = excluded.storage_created_at,
                    storage_generation = excluded.storage_generation,
                    retry_count = ops.ingestion_metadata.retry_count + 1
                """,
                [
                    raw.key,
                    raw.source_id,
                    raw.schema_version,
                    raw.subject_key,
                    raw.stream,
                    raw.logical_key,
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
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

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
        if not self.connection_usable:
            return
        self.connection.execute(
            "DELETE FROM ops.job_lock WHERE job_name = ? AND owner_id = ?", [job_name, owner_id]
        )

    def require_job_lock(self, owner_id: str, *, remaining_seconds: int = 1) -> None:
        """Stop before writing if the common loader lease is lost or nearly expired."""
        row = self.connection.execute(
            "SELECT owner_id, expires_at FROM ops.job_lock WHERE job_name='loader'"
        ).fetchone()
        if (
            row is None
            or row[0] != owner_id
            or row[1] <= datetime.now(UTC) + timedelta(seconds=remaining_seconds)
        ):
            raise RuntimeError("loader lease ownership lost or insufficient time remains")

    def finish_job(self, run_id: str, *, succeeded: bool, details: dict[str, object]) -> None:
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

    def prune_run_history(self, *, now: datetime) -> None:
        cutoff = now - timedelta(days=7)
        self.connection.execute(
            "DELETE FROM ops.job_run "
            "WHERE coalesce(completed_at, started_at) < ? "
            "AND run_id <> coalesce(( "
            "SELECT run_id FROM ops.job_run "
            "WHERE job_name IN (?, ?) AND status = 'succeeded' "
            "AND json_extract_string(details, '$.last_completed_target_date') IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 1), '')",
            [cutoff, *DAILY_JOB_HISTORY_NAMES],
        )

    def query_value(self, sql: str, parameters: list[Any] | None = None) -> Any:
        row = self.connection.execute(sql, parameters or []).fetchone()
        return None if row is None else row[0]

    def query_rows(self, sql: str, parameters: list[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.connection.execute(sql, parameters or []).fetchall()

    def finish_screen_time_collection(
        self, owner_id: str, *, heartbeats: list[tuple[str, dict[str, object]]]
    ) -> None:
        """Publish stream successes and release their lease in one transaction."""
        rows = [{"name": name, "details": json.dumps(details)} for name, details in heartbeats]
        try:
            row = self.connection.execute(
                "BEGIN TRANSACTION; "
                "SELECT owner_id,expires_at FROM ops.job_lock WHERE job_name='loader'"
            ).fetchone()
        except Exception as error:
            self.connection_usable = False
            raise WarehouseConnectionError("cannot begin collection finalization") from error
        try:
            if (
                row is None
                or row[0] != owner_id
                or row[1] <= datetime.now(UTC) + timedelta(seconds=1)
            ):
                raise RuntimeError("loader lease ownership lost or insufficient time remains")
            self.connection.execute(
                "INSERT INTO ops.heartbeat "
                "SELECT r.name::VARCHAR,?::TIMESTAMPTZ,?::VARCHAR,r.details::JSON "
                "FROM unnest(?) stream(r) ON CONFLICT (monitor_name) DO UPDATE SET "
                "succeeded_at=excluded.succeeded_at,run_id=excluded.run_id,details=excluded.details",
                [datetime.now(UTC), owner_id, rows],
            )
            self.release_job_lock("loader", owner_id)
        except Exception as error:
            try:
                self.connection.execute("ROLLBACK")
            except Exception:
                self.connection_usable = False
                raise WarehouseConnectionError("collection finalization rollback failed") from error
            raise
        try:
            self.connection.execute("COMMIT")
        except Exception as error:
            self.connection_usable = False
            raise WarehouseConnectionError(
                "collection finalization commit outcome unknown"
            ) from error

    def publish_heartbeat(self, monitor_name: str, run_id: str, details: dict[str, object]) -> None:
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

"""Transactional MotherDuck/DuckDB persistence under a shared writer lease."""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from personal_data_platform.network_diagnostics import instrument_connection, network_phase
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import DecodedBatch
from personal_data_platform.sources.fitbit.models import (
    HeartRateMinuteSnapshot,
    Snapshot,
    WriteResult,
)
from personal_data_platform.sources.fitbit.motherduck import FitbitBatch, FitbitMinuteBatch
from personal_data_platform.sources.iphone_screen_time import motherduck as iphone_screen_time
from personal_data_platform.sources.iphone_screen_time.batch import IPhoneScreenTimeBatch
from personal_data_platform.sources.mac_screen_time import motherduck as mac_screen_time
from personal_data_platform.sources.mac_screen_time.batch import MacScreenTimeBatch
from personal_data_platform.sources.owntracks import motherduck as owntracks
from personal_data_platform.sources.owntracks.batch import OwnTracksBatch
from personal_data_platform.storage.migrations import apply_migrations
from personal_data_platform.storage.warehouse import WarehouseConnectionError, WarehouseLeaseError

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

    def dbt_environment(self, *, target: str) -> dict[str, str]:
        if target == "prod":
            if not self.token:
                raise ValueError("MOTHERDUCK_TOKEN is required for the production dbt target")
            return {
                "MOTHERDUCK_DATABASE": self.database,
                "DBT_ENV_SECRET_MOTHERDUCK_TOKEN": self.token,
            }
        if target == "local":
            return {"DBT_DUCKDB_PATH": self.database}
        return {}


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


class Warehouse:
    """Commit source records atomically, with no remote ingestion receipts."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self.connection = cast("DuckDBPyConnection", instrument_connection(connection))
        self.connection_usable = True

    def close(self) -> None:
        self.connection.close()

    def migrate(self, migrations: Path | None = None) -> None:
        apply_migrations(self, migrations)

    @contextmanager
    def _transaction(self, operation: str) -> Iterator[None]:
        if not self.connection_usable:
            raise WarehouseConnectionError("reopen warehouse after an uncertain transaction")
        try:
            self.connection.execute("BEGIN TRANSACTION")
        except Exception as error:
            self.connection_usable = False
            raise WarehouseConnectionError(f"cannot begin {operation}; reopen warehouse") from error
        try:
            yield
        except BaseException:
            try:
                self.connection.execute("ROLLBACK")
            except Exception as error:
                self.connection_usable = False
                raise WarehouseConnectionError(f"{operation} rollback failed") from error
            raise
        try:
            self.connection.execute("COMMIT")
        except Exception as error:
            self.connection_usable = False
            raise WarehouseConnectionError(f"{operation} commit outcome unknown") from error

    def require_job_lock(self, owner_id: str, *, remaining_seconds: int = 1) -> None:
        """Read-only preflight; transactional writers must also fence the lease."""
        if not self.connection_usable:
            raise WarehouseConnectionError("reopen warehouse after an uncertain transaction")
        row = self.connection.execute(
            "SELECT owner_id, expires_at FROM ops.job_lock WHERE job_name='loader'"
        ).fetchone()
        if (
            row is None
            or row[0] != owner_id
            or row[1] <= datetime.now(UTC) + timedelta(seconds=remaining_seconds)
        ):
            raise WarehouseLeaseError("loader lease ownership lost or insufficient time remains")

    def fence_job_lock(self, owner_id: str, *, remaining_seconds: int = 1) -> None:
        """Write the shared row inside a transaction to conflict with takeover."""
        threshold = datetime.now(UTC) + timedelta(seconds=remaining_seconds)
        # A real expiry-column write establishes the dependency on takeover.
        # DuckDB's RETURNING path and no-op updates do not reliably do this.
        # The one-microsecond reduction never extends the lease.
        row = self.connection.execute(
            "UPDATE ops.job_lock SET expires_at=expires_at - INTERVAL 1 MICROSECOND "
            "WHERE job_name='loader' AND owner_id=? AND expires_at>?",
            [owner_id, threshold + timedelta(microseconds=1)],
        ).fetchone()
        if row is None or row[0] != 1:
            raise WarehouseLeaseError("loader lease ownership lost or insufficient time remains")

    def _remaining(self, deadline: float, monotonic: Callable[[], float]) -> int:
        seconds = deadline - monotonic()
        if seconds <= 0:
            raise TimeoutError("warehouse write deadline exceeded")
        return max(1, math.ceil(seconds))

    @contextmanager
    def _write_transaction(
        self, owner_id: str, deadline: float, *, monotonic: Callable[[], float] = time.monotonic
    ) -> Iterator[None]:
        self.require_job_lock(owner_id, remaining_seconds=self._remaining(deadline, monotonic))
        with self._transaction("source write"):
            self.fence_job_lock(owner_id, remaining_seconds=self._remaining(deadline, monotonic))
            yield
            self.fence_job_lock(owner_id, remaining_seconds=self._remaining(deadline, monotonic))

    def commit_fitbit_snapshot(
        self,
        snapshot: Snapshot | HeartRateMinuteSnapshot,
        *,
        source_key: str,
        loaded_at: datetime,
        owner_id: str,
        deadline: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> WriteResult:
        batch = (
            FitbitMinuteBatch(snapshot)
            if isinstance(snapshot, HeartRateMinuteSnapshot)
            else FitbitBatch(snapshot)
        )
        with self._write_transaction(owner_id, deadline, monotonic=monotonic):
            return batch.write_snapshot(self.connection, source_key=source_key, loaded_at=loaded_at)

    def commit_screen_time_snapshot(
        self,
        raw: RawObject,
        batch: DecodedBatch,
        *,
        invalidated_event_keys: Sequence[str],
        owner_id: str,
        deadline: float,
    ) -> int:
        if isinstance(batch, IPhoneScreenTimeBatch) and raw.source_id == "iphone_screen_time":
            with self._write_transaction(owner_id, deadline):
                return iphone_screen_time.write_state(
                    self.connection, raw, batch, invalidated_event_keys=invalidated_event_keys
                )
        if isinstance(batch, MacScreenTimeBatch) and raw.source_id == "mac_screen_time":
            with self._write_transaction(owner_id, deadline):
                return mac_screen_time.write_state(
                    self.connection, raw, batch, invalidated_event_keys=invalidated_event_keys
                )
        raise ValueError(f"unsupported Screen Time batch for source: {raw.source_id}")

    def has_screen_time_events(self, *, source_id: str) -> bool:
        if source_id not in {"iphone_screen_time", "mac_screen_time"}:
            raise ValueError(f"unsupported Screen Time source: {source_id}")
        row = self.connection.execute(
            f"SELECT EXISTS (SELECT 1 FROM base.{source_id}_event)"
        ).fetchone()
        return bool(row and row[0])

    def location_point_ids(self, ids: Iterable[str] | None = None) -> set[str]:
        """Filter incoming IDs; full inventory is reserved for reconciliation."""
        if ids is None:
            rows = self.connection.execute(
                "SELECT location_point_id FROM base.location_point"
            ).fetchall()
        else:
            keys = tuple(set(ids))
            if not keys:
                return set()
            rows = self.connection.execute(
                "SELECT location_point_id FROM base.location_point WHERE location_point_id IN (SELECT unnest(?::VARCHAR[]))",
                [keys],
            ).fetchall()
        return {str(row[0]) for row in rows}

    def load_object(
        self, raw: RawObject, *, batch: DecodedBatch, owner_id: str, deadline: float
    ) -> int:
        if not isinstance(batch, OwnTracksBatch) or raw.source_id != "owntracks":
            raise ValueError(
                "object loader supports only OwnTracks; Screen Time uses its local pending queue"
            )
        with self._write_transaction(owner_id, deadline):
            return owntracks.write_batch(self.connection, raw, batch)

    def acquire_job_lock(self, job_name: str, owner_id: str, *, lease_seconds: int) -> bool:
        import duckdb

        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=lease_seconds)
        try:
            with self._transaction("lease acquisition"):
                current = self.connection.execute(
                    "SELECT owner_id, expires_at FROM ops.job_lock WHERE job_name=?", [job_name]
                ).fetchone()
                if current is not None and current[0] != owner_id and current[1] > now:
                    return False
                if current is None:
                    self.connection.execute(
                        "INSERT INTO ops.job_lock VALUES (?, ?, ?)",
                        [job_name, owner_id, expires_at],
                    )
                else:
                    self.connection.execute(
                        "UPDATE ops.job_lock SET owner_id=?, expires_at=? WHERE job_name=?",
                        [owner_id, expires_at, job_name],
                    )
        except (duckdb.TransactionException, duckdb.ConstraintException):
            return False
        return True

    def release_job_lock(self, job_name: str, owner_id: str) -> None:
        if not self.connection_usable:
            return
        with self._transaction("lease release"):
            self.connection.execute(
                "DELETE FROM ops.job_lock WHERE job_name=? AND owner_id=?", [job_name, owner_id]
            )

    def interrupt(self) -> None:
        if self.connection_usable:
            self.connection.interrupt()

    def query_value(self, sql: str, parameters: list[Any] | None = None) -> Any:
        row = self.connection.execute(sql, parameters or []).fetchone()
        return None if row is None else row[0]

    def query_rows(self, sql: str, parameters: list[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.connection.execute(sql, parameters or []).fetchall()

    def relation_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT table_schema || '.' || table_name FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema IN ('base', 'marts', 'ops')"
        ).fetchall()
        return {row[0] for row in rows}

    def check_relation(self, relation: str) -> None:
        if re.fullmatch(r"(?:base|marts|ops)\.[a-z_][a-z0-9_]*", relation) is None:
            raise ValueError(f"invalid warehouse relation: {relation}")
        self.connection.execute(f"SELECT count(*) FROM {relation}").fetchone()

    def require_empty_target(self) -> None:
        count = self.connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_catalog = current_database() "
            "AND table_schema NOT IN ('information_schema', 'pg_catalog')"
        ).fetchone()
        if count is not None and count[0]:
            raise ValueError("rebuild target must not contain existing tables or views")

    def probe(self) -> dict[str, object]:
        """Exercise DDL and DML in the caller's isolated test database."""
        table = f"probe_{uuid4().hex}"
        self.connection.execute("CREATE SCHEMA IF NOT EXISTS preflight")
        try:
            self.connection.execute(f"CREATE TABLE preflight.{table} (value INTEGER NOT NULL)")
            self.connection.execute(f"INSERT INTO preflight.{table} VALUES (42)")
            row = self.connection.execute(f"SELECT value FROM preflight.{table}").fetchone()
            if row is None or row[0] != 42:
                raise RuntimeError("MotherDuck preflight round trip returned the wrong value")
            return {"ok": True, "tested_at": datetime.now(UTC).isoformat()}
        finally:
            self.connection.execute(f"DROP TABLE IF EXISTS preflight.{table}")

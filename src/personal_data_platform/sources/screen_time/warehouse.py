"""Screen Time receipt retention and collection completion in the warehouse."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from personal_data_platform.raw.models import RawObject
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConnectionError


def prune_receipts(
    warehouse: Warehouse, live_raw: Iterable[RawObject], *, parser_version: str
) -> None:
    """Remove superseded successes only where one live Raw is warehouse-confirmed."""

    by_scope: dict[tuple[str, str, str], list[RawObject]] = {}
    for raw in live_raw:
        if raw.source_id == "screen_time":
            by_scope.setdefault((raw.subject_key, raw.stream, raw.logical_key), []).append(raw)
    candidates = [values[0] for values in by_scope.values() if len(values) == 1]
    confirmed = warehouse.succeeded_keys_for(candidates, parser_version=parser_version)
    if not confirmed:
        return
    try:
        warehouse.connection.execute("BEGIN TRANSACTION")
    except Exception as error:
        warehouse.connection_usable = False
        raise WarehouseConnectionError("cannot begin receipt pruning; reopen warehouse") from error
    try:
        for raw in candidates:
            if raw.key not in confirmed:
                continue
            warehouse.connection.execute(
                "DELETE FROM ops.ingestion_metadata "
                "WHERE source_id = 'screen_time' AND status = 'succeeded' "
                "AND subject_key = ? AND source_stream = ? AND logical_key = ? "
                "AND (observed_at, object_key) < (?, ?)",
                [raw.subject_key, raw.stream, raw.logical_key, raw.observed_at, raw.key],
            )
    except Exception as error:
        try:
            warehouse.connection.execute("ROLLBACK")
        except Exception:
            warehouse.connection_usable = False
            raise WarehouseConnectionError("receipt pruning rollback failed") from error
        raise
    try:
        warehouse.connection.execute("COMMIT")
    except Exception as error:
        warehouse.connection_usable = False
        raise WarehouseConnectionError("receipt pruning commit outcome unknown") from error


def finish_collection(
    warehouse: Warehouse, owner_id: str, *, heartbeats: list[tuple[str, dict[str, object]]]
) -> None:
    """Publish stream successes and release their lease in one transaction."""
    rows = [{"name": name, "details": json.dumps(details)} for name, details in heartbeats]
    try:
        row = warehouse.connection.execute(
            "BEGIN TRANSACTION; "
            "SELECT owner_id,expires_at FROM ops.job_lock WHERE job_name='loader'"
        ).fetchone()
    except Exception as error:
        warehouse.connection_usable = False
        raise WarehouseConnectionError("cannot begin collection finalization") from error
    try:
        if row is None or row[0] != owner_id or row[1] <= datetime.now(UTC) + timedelta(seconds=1):
            raise RuntimeError("loader lease ownership lost or insufficient time remains")
        warehouse.connection.execute(
            "INSERT INTO ops.heartbeat "
            "SELECT r.name::VARCHAR,?::TIMESTAMPTZ,?::VARCHAR,r.details::JSON "
            "FROM unnest(?) stream(r) ON CONFLICT (monitor_name) DO UPDATE SET "
            "succeeded_at=excluded.succeeded_at,run_id=excluded.run_id,details=excluded.details",
            [datetime.now(UTC), owner_id, rows],
        )
        warehouse.release_job_lock("loader", owner_id)
    except Exception as error:
        try:
            warehouse.connection.execute("ROLLBACK")
        except Exception:
            warehouse.connection_usable = False
            raise WarehouseConnectionError("collection finalization rollback failed") from error
        raise
    try:
        warehouse.connection.execute("COMMIT")
    except Exception as error:
        warehouse.connection_usable = False
        raise WarehouseConnectionError("collection finalization commit outcome unknown") from error

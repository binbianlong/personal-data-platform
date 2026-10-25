"""Screen Time receipt retention in the warehouse."""

from __future__ import annotations

from collections.abc import Iterable

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

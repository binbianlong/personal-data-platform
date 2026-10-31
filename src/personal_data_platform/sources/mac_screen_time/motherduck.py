"""Source-owned warehouse write and receipt entry points."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING

from personal_data_platform.biome import motherduck as _shared
from personal_data_platform.raw.models import RawObject

from .batch import MacScreenTimeBatch

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection
from .raw import SOURCE_ID


def write_state(
    connection: DuckDBPyConnection,
    raw: RawObject,
    batch: MacScreenTimeBatch,
    *,
    loaded_at: datetime,
) -> None:
    _shared.write_state(connection, raw, batch, loaded_at=loaded_at, source_id=SOURCE_ID)


def receipt_candidates(live_raw: Iterable[RawObject]) -> list[RawObject]:
    return _shared.receipt_candidates(live_raw, source_id=SOURCE_ID)


def prune_receipts(connection: DuckDBPyConnection, confirmed: Iterable[RawObject]) -> None:
    _shared.prune_receipts(connection, confirmed, source_id=SOURCE_ID)


def current_object_keys(connection: DuckDBPyConnection, *, stream: str) -> set[str]:
    return _shared.current_object_keys(connection, source_id=SOURCE_ID, stream=stream)

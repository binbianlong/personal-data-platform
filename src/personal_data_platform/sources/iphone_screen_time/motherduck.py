"""Source-owned event statements; the caller owns the transaction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from personal_data_platform.biome import motherduck as _shared
from personal_data_platform.raw.models import RawObject

from .batch import IPhoneScreenTimeBatch
from .raw import SOURCE_ID

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def write_state(
    connection: DuckDBPyConnection,
    raw: RawObject,
    batch: IPhoneScreenTimeBatch,
    *,
    invalidated_event_keys: Sequence[str] = (),
) -> int:
    return _shared.write_state(
        connection, raw, batch, source_id=SOURCE_ID, invalidated_event_keys=invalidated_event_keys
    )

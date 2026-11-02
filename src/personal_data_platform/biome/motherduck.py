"""Source event statements executed inside the warehouse's fenced transaction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from personal_data_platform.raw.models import RawObject

from .models import ParsedScreenTimeRecord

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


class BiomeBatch(Protocol):
    @property
    def segment_kind(self) -> str | None: ...
    @property
    def records(self) -> Sequence[ParsedScreenTimeRecord]: ...


EVENT_COLUMNS = ("event_key", "device_key", "bundle_id", "event_at", "state", "is_valid")


def adopted_records(batch: BiomeBatch) -> list[ParsedScreenTimeRecord]:
    """Resolve metadata slots before deduplicating valid source events."""
    if batch.segment_kind == "tombstones":
        return []
    positions: set[int] = set()
    latest: dict[int, ParsedScreenTimeRecord] = {}
    for record in batch.records:
        if record.record_metadata_offset in positions:
            raise ValueError("duplicate metadata position in Screen Time observation")
        positions.add(record.record_metadata_offset)
        prior = latest.get(record.record_offset)
        if prior is None or record.record_metadata_offset > prior.record_metadata_offset:
            latest[record.record_offset] = record
    events: dict[str, ParsedScreenTimeRecord] = {}
    for record in latest.values():
        if (
            record.record_state.upper() == "WRITTEN"
            and record.crc_passed is not False
            and record.record_kind == "event"
            and record.event_key is not None
            and record.bundle_id is not None
            and record.event_at is not None
            and record.in_foreground is not None
        ):
            events[record.event_key] = record
    return list(events.values())


def write_state(
    connection: DuckDBPyConnection,
    raw: RawObject,
    batch: BiomeBatch,
    *,
    source_id: str,
    invalidated_event_keys: Sequence[str] = (),
) -> int:
    if source_id not in {"iphone_screen_time", "mac_screen_time"} or raw.source_id != source_id:
        raise ValueError("invalid Screen Time source")
    if batch.segment_kind == "tombstones":
        return 0
    records = adopted_records(batch)
    table = f"base.{source_id}_event"
    count = 0
    adopted = {r.event_key for r in records}
    invalidated = sorted(set(invalidated_event_keys) - adopted)
    if invalidated:
        count += len(
            connection.execute(
                f"UPDATE {table} SET is_valid = false WHERE is_valid AND event_key IN (SELECT unnest(?::VARCHAR[])) RETURNING event_key",
                [invalidated],
            ).fetchall()
        )
    if records:
        rows = [
            (
                r.event_key,
                r.device_key,
                r.bundle_id,
                r.event_at,
                "start" if r.in_foreground else "end",
                True,
            )
            for r in records
        ]
        columns = [list(values) for values in zip(*rows, strict=True)]
        types = ("VARCHAR", "VARCHAR", "VARCHAR", "TIMESTAMPTZ", "VARCHAR", "BOOLEAN")
        count += len(
            connection.execute(
                f"INSERT INTO {table} ({', '.join(EVENT_COLUMNS)}) SELECT "
                + ", ".join(f"unnest(?::{kind}[])" for kind in types)
                + " ON CONFLICT (event_key) DO UPDATE SET "
                + ", ".join(f"{column} = excluded.{column}" for column in EVENT_COLUMNS[1:])
                + " WHERE "
                + " OR ".join(
                    f"{table}.{column} IS DISTINCT FROM excluded.{column}"
                    for column in EVENT_COLUMNS[1:]
                )
                + " RETURNING event_key",
                columns,
            ).fetchall()
        )
    return count

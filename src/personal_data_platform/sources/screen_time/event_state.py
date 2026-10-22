"""Maintain current Screen Time copies and durable analytical history."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from personal_data_platform.raw.models import RawObject

from .models import ParsedScreenTimeRecord

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from .writer import ScreenTimeBatch

LOGGER = logging.getLogger(__name__)
RECORD_COLUMNS = (
    "physical_id",
    "device_key",
    "source_stream",
    "segment_key",
    "record_offset",
    "record_metadata_offset",
    "event_key",
    "bundle_id",
    "event_at",
    "state",
    "transition_reason",
    "kind",
    "app_version",
    "app_build",
    "platform_flag",
    "parser_version",
    "unknown_field_count",
    "segment_filename",
)
EVENT_COLUMNS = (
    "event_key",
    "device_key",
    "platform",
    "source_stream",
    "bundle_id",
    "event_at",
    "state",
    "kind",
    "object_key",
    "segment_key",
    "observed_at",
    "parser_version",
    "unknown_field_count",
    "duplicate_occurrence_count",
)
ANALYTICAL_COLUMNS = tuple(
    name
    for name in EVENT_COLUMNS
    if name
    not in {
        "object_key",
        "segment_key",
        "observed_at",
    }
)


def write_state(
    connection: DuckDBPyConnection,
    raw: RawObject,
    batch: ScreenTimeBatch,
    *,
    loaded_at: datetime,
) -> None:
    scope = [raw.subject_key, raw.stream, raw.logical_key]
    connection.execute("""
        CREATE OR REPLACE TEMP TABLE screen_time_affected_event (event_key VARCHAR PRIMARY KEY);
        CREATE OR REPLACE TEMP TABLE screen_time_input AS
        SELECT *, NULL::DOUBLE AS record_timestamp_cocoa, NULL::VARCHAR AS digest
        FROM ops.screen_time_record WHERE false;
    """)
    # Apple deletion markers have no role in the current-record model.
    if batch.segment_kind == "tombstones":
        return

    previous = connection.execute(
        "SELECT observed_at, object_key FROM ops.screen_time_segment "
        "WHERE device_key = ? AND source_stream = ? AND segment_key = ?",
        scope,
    ).fetchone()
    if previous is not None and (raw.observed_at, raw.key) < previous:
        return

    # The highest metadata entry defines a physical slot, even when erased or invalid.
    positions = set()
    latest: dict[int, ParsedScreenTimeRecord] = {}
    for record in batch.records:
        if record.record_metadata_offset in positions:
            raise ValueError("duplicate metadata position in Screen Time observation")
        positions.add(record.record_metadata_offset)
        prior = latest.get(record.record_offset)
        if prior is None or record.record_metadata_offset > prior.record_metadata_offset:
            latest[record.record_offset] = record

    rows = []
    for record in latest.values():
        if (
            record.record_state.upper() != "WRITTEN"
            or record.crc_passed is False
            or record.record_kind != "event"
            or record.event_key is None
        ):
            continue
        digest = hashlib.sha256(record.original_payload).hexdigest()
        rows.append(
            [
                digest,
                *scope,
                record.record_offset,
                record.record_metadata_offset,
                record.event_key,
                record.bundle_id,
                record.event_at,
                "start" if record.in_foreground else "end",
                record.transition_reason,
                record.kind,
                record.app_version,
                record.app_build,
                record.platform_flag,
                record.parser_version,
                record.unknown_field_count,
                batch.source_segment_name or record.segment_filename,
                record.record_timestamp_cocoa,
                digest,
            ]
        )
    if rows:
        # One columnar statement avoids a remote statement per physical record.
        columns = [list(column) for column in zip(*rows, strict=True)]
        connection.execute(
            "INSERT INTO screen_time_input SELECT " + ",".join("unnest(?)" for _ in columns),
            columns,
        )
        connection.execute("""
            UPDATE screen_time_input SET physical_id = ops.screen_time_physical_id(
                device_key, source_stream, segment_key, record_offset, record_metadata_offset,
                record_timestamp_cocoa, digest
            );
        """)
    connection.execute(
        "ALTER TABLE screen_time_input DROP COLUMN record_timestamp_cocoa; "
        "ALTER TABLE screen_time_input DROP COLUMN digest"
    )

    connection.execute(
        "CREATE OR REPLACE TEMP TABLE screen_time_previous AS "
        "SELECT * FROM ops.screen_time_record WHERE device_key = ? "
        "AND source_stream = ? AND segment_key = ?",
        scope,
    )
    distinct = " OR ".join(
        f"old.{column} IS DISTINCT FROM incoming.{column}"
        for column in RECORD_COLUMNS
        if column != "physical_id"
    )
    connection.execute(f"""
        CREATE OR REPLACE TEMP TABLE screen_time_changes AS
        SELECT old.event_key AS old_event_key, incoming.event_key AS new_event_key,
               incoming.physical_id AS new_physical_id
        FROM screen_time_previous old FULL JOIN screen_time_input incoming USING (physical_id)
        WHERE old.physical_id IS NULL OR incoming.physical_id IS NULL OR {distinct};
        INSERT OR IGNORE INTO screen_time_affected_event
        SELECT old_event_key FROM screen_time_changes WHERE old_event_key IS NOT NULL
        UNION SELECT new_event_key FROM screen_time_changes WHERE new_event_key IS NOT NULL;
    """)
    # A later observation can change which duplicate copy is representative.
    if previous is None or (raw.observed_at, raw.key) > previous:
        connection.execute(
            "INSERT OR IGNORE INTO screen_time_affected_event "
            "SELECT DISTINCT event_key FROM screen_time_input"
        )

    connection.execute(
        "INSERT INTO ops.screen_time_segment VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (device_key, source_stream, segment_key) DO UPDATE SET "
        "observed_at = excluded.observed_at, object_key = excluded.object_key "
        "WHERE (excluded.observed_at, excluded.object_key) > "
        "(ops.screen_time_segment.observed_at, ops.screen_time_segment.object_key)",
        [*scope, raw.observed_at, raw.key],
    )

    # Preserve historical values before removing their last current physical copy.
    connection.execute(
        "UPDATE base.screen_time_event e SET is_active = false, loaded_at = ? "
        "WHERE e.is_active AND e.event_key IN "
        "(SELECT event_key FROM screen_time_previous) "
        "AND NOT EXISTS (SELECT 1 FROM screen_time_input i WHERE i.event_key = e.event_key) "
        "AND NOT EXISTS (SELECT 1 FROM ops.screen_time_record r "
        "WHERE r.event_key = e.event_key AND "
        "(r.device_key, r.source_stream, r.segment_key) <> (?, ?, ?))",
        [loaded_at, *scope],
    )
    connection.execute(
        "DELETE FROM ops.screen_time_record WHERE device_key = ? AND source_stream = ? "
        "AND segment_key = ? AND physical_id NOT IN "
        "(SELECT physical_id FROM screen_time_input)",
        scope,
    )
    update_columns = ", ".join(
        f"{column} = excluded.{column}" for column in RECORD_COLUMNS if column != "physical_id"
    )
    connection.execute(
        "INSERT INTO ops.screen_time_record SELECT incoming.* FROM screen_time_input incoming "
        "WHERE incoming.physical_id IN "
        "(SELECT new_physical_id FROM screen_time_changes WHERE new_physical_id IS NOT NULL) "
        "ON CONFLICT (physical_id) DO UPDATE SET " + update_columns
    )

    event_columns = (*EVENT_COLUMNS, "is_active", "loaded_at")
    connection.execute(
        f"INSERT INTO base.screen_time_event ({', '.join(event_columns)}) "
        "SELECT *, ? FROM ops.screen_time_resolve("
        "(SELECT list(event_key) FROM screen_time_affected_event)) "
        "ON CONFLICT (event_key) DO UPDATE SET "
        + ", ".join(
            f"{column} = excluded.{column}" for column in event_columns if column != "event_key"
        )
        + " WHERE "
        + " OR ".join(
            f"base.screen_time_event.{column} IS DISTINCT FROM excluded.{column}"
            for column in (*ANALYTICAL_COLUMNS, "is_active")
        ),
        [loaded_at],
    )
    count_row = connection.execute("SELECT count(*) FROM screen_time_affected_event").fetchone()
    assert count_row is not None
    LOGGER.info("Screen Time affected events=%d", count_row[0])

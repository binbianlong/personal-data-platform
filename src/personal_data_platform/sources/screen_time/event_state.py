"""Update only the affected Screen Time state inside the caller's transaction."""

from __future__ import annotations

import hashlib
import logging

LOGGER = logging.getLogger(__name__)
EVENT_COLUMNS = (
    "event_key",
    "device_key",
    "platform",
    "source_stream",
    "bundle_id",
    "event_at",
    "state",
    "transition_reason",
    "kind",
    "app_version",
    "app_build",
    "platform_flag",
    "object_key",
    "segment_key",
    "segment_filename",
    "record_offset",
    "record_metadata_offset",
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
        "segment_filename",
        "record_offset",
        "record_metadata_offset",
        "observed_at",
    }
)


def write_state(connection, raw, batch, *, loaded_at):
    scope = [raw.subject_key, raw.stream, raw.logical_key]
    # Temporary working sets contain only this segment and its related deletions/events.
    connection.execute("""
        CREATE OR REPLACE TEMP TABLE screen_time_affected_event (event_key VARCHAR PRIMARY KEY);
        CREATE OR REPLACE TEMP TABLE screen_time_affected_tombstone (physical_id VARCHAR PRIMARY KEY);
        CREATE OR REPLACE TEMP TABLE screen_time_input AS
        SELECT * EXCLUDE (is_current, is_valid), NULL::VARCHAR AS record_kind,
               NULL::VARCHAR AS target_segment_name, NULL::UBIGINT AS target_offset,
               NULL::UINTEGER AS target_length, NULL::DOUBLE AS target_event_timestamp,
               NULL::UINTEGER AS deletion_reason
        FROM ops.screen_time_record WHERE false;
    """)
    # Capture old name matches as well, before learning a new/ambiguous v2 name.
    _affected_tombstones(connection, scope)
    connection.execute(
        """
        INSERT INTO ops.screen_time_segment
        VALUES (?, ?, ?, ?, ?, ?, false)
        ON CONFLICT (device_key, source_stream, segment_key) DO UPDATE SET
            source_segment_name = coalesce(ops.screen_time_segment.source_segment_name,
                                           excluded.source_segment_name),
            name_ambiguous = ops.screen_time_segment.name_ambiguous OR (
                ops.screen_time_segment.source_segment_name IS NOT NULL
                AND excluded.source_segment_name IS NOT NULL
                AND ops.screen_time_segment.source_segment_name <> excluded.source_segment_name
            ),
            observed_at = greatest(ops.screen_time_segment.observed_at, excluded.observed_at),
            object_key = CASE
                WHEN (excluded.observed_at, excluded.object_key) >=
                     (ops.screen_time_segment.observed_at, ops.screen_time_segment.object_key)
                THEN excluded.object_key ELSE ops.screen_time_segment.object_key END
        """,
        [
            *scope,
            raw.observed_at,
            raw.key,
            batch.source_segment_name if batch.segment_kind == "events" else None,
        ],
    )
    # The highest metadata entry defines a physical slot, including erased/bad CRC entries.
    positions = set()
    latest = {}
    for record in batch.records:
        if record.record_metadata_offset in positions:
            raise ValueError("duplicate metadata position in Screen Time observation")
        positions.add(record.record_metadata_offset)
        previous = latest.get(record.record_offset)
        if previous is None or record.record_metadata_offset > previous.record_metadata_offset:
            latest[record.record_offset] = record
    rows = []
    for r in latest.values():
        if r.record_state.upper() != "WRITTEN" or r.crc_passed is False:
            continue
        if r.record_kind not in ("event", "tombstone"):
            continue
        if r.record_kind == "event" and r.event_key is None:
            continue
        rows.append(
            [
                hashlib.sha256(r.original_payload).hexdigest(),
                *scope,
                r.record_offset,
                r.record_metadata_offset,
                r.payload_length or len(r.original_payload),
                r.record_timestamp_cocoa,
                r.event_key,
                r.bundle_id,
                r.event_at,
                "start" if r.in_foreground else "end",
                r.transition_reason,
                r.kind,
                r.app_version,
                r.app_build,
                r.platform_flag,
                r.parser_version,
                r.unknown_field_count,
                batch.source_segment_name or r.segment_filename,
                raw.observed_at,
                raw.key,
                r.record_kind,
                r.target_segment_name,
                r.target_offset,
                r.target_length,
                r.target_event_timestamp,
                r.deletion_reason,
            ]
        )
    if rows:
        connection.executemany(
            "INSERT INTO screen_time_input VALUES (" + ",".join("?" for _ in rows[0]) + ")", rows
        )
    connection.execute("""
        UPDATE screen_time_input SET physical_id = ops.screen_time_physical_id(
            device_key, source_stream, segment_key, record_offset, record_metadata_offset,
            record_timestamp_cocoa, physical_id
        );
    """)
    # Keep old keys before corrections overwrite them. Older evidence rejected by
    # the UPSERT cannot change ranking and must not expand the resolve set.
    connection.execute(
        """
        INSERT OR IGNORE INTO screen_time_affected_event
        SELECT r.event_key FROM ops.screen_time_record r
        JOIN ops.screen_time_segment s USING (device_key, source_stream, segment_key)
        WHERE r.device_key = ? AND r.source_stream = ? AND r.segment_key = ?
          AND ((r.is_valid AND r.object_key = ?) OR (r.is_current AND s.object_key = ?));
        """,
        [*scope, raw.key, raw.key],
    )
    connection.execute("""
        CREATE OR REPLACE TEMP TABLE screen_time_updated_event AS
        SELECT i.event_key, r.event_key AS old_event_key
        FROM screen_time_input i LEFT JOIN ops.screen_time_record r USING (physical_id)
        WHERE i.record_kind = 'event' AND (r.physical_id IS NULL OR
              (i.observed_at, i.object_key) >= (r.observed_at, r.object_key));
        INSERT OR IGNORE INTO screen_time_affected_event
        SELECT event_key FROM screen_time_updated_event
        UNION SELECT old_event_key FROM screen_time_updated_event WHERE old_event_key IS NOT NULL;
    """)
    # Include both old and newly learned target names, then preserve effects before
    # either record keys or tombstone reasons can be corrected in place.
    _affected_tombstones(connection, scope)
    _capture_deletion_effects(connection, "screen_time_previous_effect")
    _replace_checkpoint_versions(connection)
    # Reparse corrections invalidate only versions whose latest evidence is this Raw.
    # Later observations of the same bytes remain valid.
    for table in ("record", "tombstone"):
        connection.execute(
            f"UPDATE ops.screen_time_{table} SET is_valid = false WHERE "
            "device_key = ? AND source_stream = ? AND segment_key = ? AND object_key = ? "
            "AND is_valid",
            [*scope, raw.key],
        )
    connection.execute(
        """
        UPDATE ops.screen_time_record r SET is_current = false
        FROM ops.screen_time_segment s
        WHERE r.device_key = s.device_key AND r.source_stream = s.source_stream
          AND r.segment_key = s.segment_key AND s.device_key = ? AND s.source_stream = ?
          AND s.segment_key = ? AND s.object_key = ? AND r.is_current
        """,
        [*scope, raw.key],
    )
    columns = [
        row[0]
        for row in connection.execute("SELECT * FROM ops.screen_time_record LIMIT 0").description
    ]
    connection.execute(
        "INSERT INTO ops.screen_time_record SELECT i.* EXCLUDE "
        "(record_kind, target_segment_name, target_offset, target_length, "
        "target_event_timestamp, deletion_reason), i.object_key = s.object_key, true "
        "FROM screen_time_input i JOIN ops.screen_time_segment s "
        "USING (device_key, source_stream, segment_key) WHERE i.record_kind = 'event' "
        "ON CONFLICT (physical_id) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in columns if c != "physical_id")
        + " WHERE (excluded.observed_at, excluded.object_key) >= "
        "(ops.screen_time_record.observed_at, ops.screen_time_record.object_key)"
    )
    connection.execute("""
        INSERT INTO ops.screen_time_tombstone
        SELECT physical_id, device_key, source_stream, segment_key, target_segment_name,
               target_offset, target_length, target_event_timestamp, deletion_reason,
               observed_at, object_key, true, 'unmatched'
        FROM screen_time_input WHERE record_kind = 'tombstone'
        ON CONFLICT (physical_id) DO UPDATE SET
            target_segment_name = excluded.target_segment_name,
            target_offset = excluded.target_offset, target_length = excluded.target_length,
            target_event_timestamp = excluded.target_event_timestamp,
            deletion_reason = excluded.deletion_reason, observed_at = excluded.observed_at,
            object_key = excluded.object_key, is_valid = true
        WHERE (excluded.observed_at, excluded.object_key) >=
              (ops.screen_time_tombstone.observed_at, ops.screen_time_tombstone.object_key)
    """)
    _affected_tombstones(connection, scope)
    connection.execute("""
        DELETE FROM ops.screen_time_deletion_match
        WHERE tombstone_id IN (SELECT physical_id FROM screen_time_affected_tombstone);
        INSERT INTO ops.screen_time_deletion_match
        SELECT * FROM ops.screen_time_matching_record(
            (SELECT list(physical_id) FROM screen_time_affected_tombstone)
        );
        UPDATE ops.screen_time_tombstone t SET resolution = CASE
            WHEN NOT is_valid THEN 'invalidated'
            WHEN deletion_reason NOT IN (1, 2) OR deletion_reason IS NULL THEN 'unsupported_reason'
            WHEN NOT EXISTS (SELECT 1 FROM ops.screen_time_deletion_match m
                             WHERE m.tombstone_id = t.physical_id) THEN 'unmatched'
            WHEN deletion_reason = 1 THEN 'ttl_history_retained'
            ELSE 'user_deletion_applied' END
        WHERE t.physical_id IN (SELECT physical_id FROM screen_time_affected_tombstone);
    """)
    _capture_deletion_effects(connection, "screen_time_next_effect")
    connection.execute("""
        INSERT OR IGNORE INTO screen_time_affected_event
        SELECT event_key FROM (
            (SELECT * FROM screen_time_previous_effect EXCEPT SELECT * FROM screen_time_next_effect)
            UNION
            (SELECT * FROM screen_time_next_effect EXCEPT SELECT * FROM screen_time_previous_effect)
        );
    """)
    columns = (*EVENT_COLUMNS, "is_active", "loaded_at")
    connection.execute(
        f"INSERT INTO base.screen_time_event ({', '.join(columns)}) "
        "SELECT *, ? FROM ops.screen_time_resolve("
        "(SELECT list(event_key) FROM screen_time_affected_event)) "
        "ON CONFLICT (event_key) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in columns if c != "event_key")
        + " WHERE "
        + " OR ".join(
            f"base.screen_time_event.{c} IS DISTINCT FROM excluded.{c}"
            for c in (*ANALYTICAL_COLUMNS, "is_active")
        ),
        [loaded_at],
    )
    # A parser correction can change an event key at an unchanged physical location.
    connection.execute(
        "UPDATE base.screen_time_event e SET is_active = false, loaded_at = ? "
        "WHERE e.is_active AND e.event_key IN (SELECT event_key FROM screen_time_affected_event) "
        "AND NOT EXISTS (SELECT 1 FROM ops.screen_time_record r WHERE r.event_key = e.event_key)",
        [loaded_at],
    )
    counts = connection.execute("""
        SELECT (SELECT count(*) FROM screen_time_affected_event),
               (SELECT count(*) FROM screen_time_affected_tombstone)
    """).fetchone()
    LOGGER.info("Screen Time affected events=%d tombstones=%d", *counts)


def _affected_tombstones(connection, scope):
    connection.execute(
        """
        INSERT OR IGNORE INTO screen_time_affected_tombstone
        SELECT t.physical_id FROM ops.screen_time_tombstone t
        WHERE t.device_key = ? AND t.source_stream = ? AND (
            t.segment_key = ? OR t.target_segment_name IN (
                SELECT source_segment_name FROM ops.screen_time_segment
                WHERE device_key = ? AND source_stream = ? AND segment_key = ?
            )
        )
        """,
        [*scope, *scope],
    )


def _replace_checkpoint_versions(connection):
    # Checkpoints lacked payload digests. Once equivalent decoded evidence arrives
    # from Raw, retire its imported version in favor of the ordinary physical ID.
    # Otherwise a later parser correction would leave the imported deletion alive.
    common = ("device_key", "source_stream", "segment_key")
    fields = {
        "record": (
            "record_offset",
            "record_metadata_offset",
            "payload_length",
            "record_timestamp_cocoa",
            "event_key",
            "bundle_id",
            "event_at",
            "state",
            "transition_reason",
            "kind",
            "app_version",
            "app_build",
            "platform_flag",
        ),
        "tombstone": (
            "target_segment_name",
            "target_offset",
            "target_length",
            "target_event_timestamp",
            "deletion_reason",
        ),
    }
    for table, columns in fields.items():
        equality = " AND ".join(
            f"r.{column} IS NOT DISTINCT FROM i.{column}" for column in (*common, *columns)
        )
        if table == "tombstone":
            for index, (column, kind) in enumerate(
                (
                    ("record_offset", "UBIGINT"),
                    ("record_metadata_offset", "UBIGINT"),
                    ("payload_length", "UINTEGER"),
                    ("record_timestamp_cocoa", "DOUBLE"),
                )
            ):
                equality += (
                    f" AND try_cast(json_extract(try_cast(split_part(r.physical_id, ':', 2) "
                    f"AS JSON), '$[{index}]') AS {kind}) IS NOT DISTINCT FROM i.{column}"
                )
        connection.execute(
            f"UPDATE ops.screen_time_{table} r SET is_valid = false "
            "FROM screen_time_input i WHERE starts_with(r.physical_id, 'checkpoint:') "
            "AND r.is_valid AND i.record_kind = ? "
            "AND (i.observed_at, i.object_key) >= (r.observed_at, r.object_key) AND " + equality,
            ["event" if table == "record" else table],
        )


def _capture_deletion_effects(connection, table):
    connection.execute(f"""
        CREATE OR REPLACE TEMP TABLE {table} AS
        SELECT m.tombstone_id, m.physical_id, t.deletion_reason, r.event_key
        FROM ops.screen_time_deletion_match m
        JOIN ops.screen_time_tombstone t ON t.physical_id = m.tombstone_id
        JOIN ops.screen_time_record r ON r.physical_id = m.physical_id
        WHERE m.tombstone_id IN (SELECT physical_id FROM screen_time_affected_tombstone)
    """)

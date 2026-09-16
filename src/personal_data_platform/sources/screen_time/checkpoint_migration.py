"""One-time import of format-1 ingestion checkpoints before migration 006 resolves events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

IMPORT_POINT = "-- Name uniqueness and all physical coordinates are required for deletion matching."
SCOPE = ("device_key", "source_stream", "segment_key")
PROVENANCE = {"object_key", "segment_sha256", "observed_at", "segment_filename", "original_payload"}


def _encode(value):
    return json.dumps(value, default=lambda v: v.isoformat(), sort_keys=True, separators=(",", ":"))


def migrate_checkpoint(connection, sql: str, path: Path | None) -> None:
    """Called inside the migration transaction; never change the source checkpoint."""
    marker = connection.execute(
        "SELECT state_id, revision FROM ops.screen_time_checkpoint WHERE singleton"
    ).fetchone()
    if marker is None and path is None:
        connection.execute(sql)
        return
    if path is None:
        raise RuntimeError(
            "Screen Time checkpoint required: run pdp screen-time migrate-checkpoint --checkpoint PATH"
        )
    if marker is None:
        raise RuntimeError("Screen Time checkpoint has no matching warehouse marker")
    with closing(sqlite3.connect(":memory:")) as checkpoint:
        checkpoint.deserialize(path.read_bytes())
        checkpoint.execute("PRAGMA query_only = ON")
        if checkpoint.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("corrupt Screen Time checkpoint")
        metadata = {
            key: json.loads(value)
            for key, value in checkpoint.execute("SELECT key, value FROM metadata")
        }
        if metadata.get("format") != 1:
            raise RuntimeError("unsupported Screen Time checkpoint format")
        if (metadata.get("state_id"), metadata.get("revision")) != marker:
            raise RuntimeError("Screen Time checkpoint identity/revision differs from warehouse")
        if metadata.get("pending") is not None:
            raise RuntimeError("recover pending Screen Time checkpoint with the old runtime first")
        prefix, separator, suffix = sql.partition(IMPORT_POINT)
        if not separator:
            raise RuntimeError("Screen Time checkpoint migration import point is missing")
        connection.execute(prefix)
        _import_state(connection, checkpoint)
        # The old writer retained the last active copy count on deletion. The new
        # resolver counts active copies, so inactive events have zero duplicates.
        # The following merge guard still verifies that every deletion is reproduced.
        connection.execute("""
            UPDATE base.screen_time_event SET duplicate_occurrence_count = 0,
                loaded_at = current_timestamp
            WHERE NOT is_active AND duplicate_occurrence_count <> 0
        """)
        connection.execute(separator + suffix)


def _import_state(connection, checkpoint):
    from .models import ParsedScreenTimeRecord

    # Checkpoint records omit payload bytes. Reuse original physical identities when
    # archived decoded fields match; otherwise use a namespaced normalized identity.
    cursor = connection.execute("""
        SELECT *, ops.screen_time_physical_id(
            device_key, source_stream, segment_key, record_offset, record_metadata_offset,
            record_timestamp_cocoa, sha256(original_payload)
        ) AS physical_id FROM base.screen_time_record_occurrence
    """)
    columns = [column[0] for column in cursor.description]
    identities = {}
    for row in cursor.fetchall():
        record = dict(zip(columns, row, strict=True))
        normalized = {
            k: v
            for k, v in record.items()
            if k in ParsedScreenTimeRecord.__dataclass_fields__ and k not in PROVENANCE
        }
        identities[hashlib.sha256(_encode(normalized).encode()).hexdigest()] = record["physical_id"]

    values = {}
    for key, document in checkpoint.execute("SELECT id, document FROM record_values"):
        if hashlib.sha256(document.encode()).hexdigest() != key:
            raise RuntimeError("Screen Time checkpoint record digest mismatch")
        values[key] = json.loads(document)
    observations = list(
        checkpoint.execute(
            "SELECT object_key, scope, ordering, document FROM observations ORDER BY scope, ordering"
        )
    )
    keys = {row[0] for row in observations}
    required = connection.execute("""
        SELECT object_key FROM base.screen_time_segment_observation
        UNION SELECT object_key FROM base.screen_time_record_occurrence
        UNION SELECT object_key FROM ops.ingestion_metadata
        WHERE source_id = 'screen_time' AND status = 'succeeded'
    """).fetchall()
    if any(key not in keys for (key,) in required):
        raise RuntimeError("Screen Time checkpoint is missing warehouse observations")
    changes = {}
    for key, offset, record_id in checkpoint.execute(
        "SELECT object_key, offset, record_id FROM changes"
    ):
        if key not in keys or (record_id is not None and record_id not in values):
            raise RuntimeError("Screen Time checkpoint has dangling changes")
        changes.setdefault(key, []).append((offset, record_id))

    segments, records, tombstones = {}, {}, {}
    previous_scope, snapshot = None, {}
    for key, scope, ordering, document in observations:
        observation = json.loads(document)
        scope_tuple = tuple(observation[k] for k in SCOPE)
        if (
            key != observation["object_key"]
            or scope != _encode(scope_tuple)
            or ordering != observation["observed_at"] + "\x1f" + key
        ):
            raise RuntimeError("Screen Time checkpoint observation identity mismatch")
        if scope != previous_scope:
            snapshot = {}
            previous_scope = scope
        for offset, record_id in changes.get(key, []):
            if record_id is None:
                snapshot.pop(offset, None)
            else:
                snapshot[offset] = record_id
        segment = segments.setdefault(scope_tuple, {"names": set()})
        segment.update(observed_at=observation["observed_at"], object_key=key, current=set())
        if observation.get("segment_kind") == "events" and observation.get("source_segment_name"):
            segment["names"].add(observation["source_segment_name"])
        latest = {}
        for offset, record_id in snapshot.items():
            record = values[record_id]
            if (
                tuple(record[k] for k in SCOPE) != scope_tuple
                or record["record_metadata_offset"] != offset
            ):
                raise RuntimeError("Screen Time checkpoint record scope/offset mismatch")
            position = record["record_offset"]
            if position not in latest or offset > latest[position][0]:
                latest[position] = (offset, record_id)
        for _, record_id in latest.values():
            record = values[record_id]
            if record["record_state"].upper() != "WRITTEN" or record["crc_passed"] is False:
                continue
            # Tombstone tables omit their own physical coordinates. Keep them in
            # the imported ID so a future Raw can replace only this exact version.
            position = _encode(
                [
                    record[k]
                    for k in (
                        "record_offset",
                        "record_metadata_offset",
                        "payload_length",
                        "record_timestamp_cocoa",
                    )
                ]
            )
            physical_id = identities.get(record_id, f"checkpoint:{position}:{record_id}")
            candidate = {
                **record,
                **{k: observation[k] for k in ("object_key", "observed_at", "segment_filename")},
                "physical_id": physical_id,
            }
            if record["record_kind"] == "event" and record["event_key"] is not None:
                records[physical_id] = candidate
                segment["current"].add(physical_id)
            elif record["record_kind"] == "tombstone":
                tombstones[physical_id] = candidate

    for scope, segment in segments.items():
        # Retain archived physical identities for the 007 completeness check, but
        # their old parser output is no longer authoritative for this scope.
        for table in ("record", "tombstone"):
            connection.execute(
                f"UPDATE ops.screen_time_{table} SET is_valid = false "
                "WHERE device_key = ? AND source_stream = ? AND segment_key = ?",
                list(scope),
            )
        names = sorted(segment["names"])
        connection.execute(
            "INSERT OR REPLACE INTO ops.screen_time_segment VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                *scope,
                segment["observed_at"],
                segment["object_key"],
                names[0] if names else None,
                len(names) > 1,
            ],
        )
    for record in records.values():
        scope = tuple(record[k] for k in SCOPE)
        row = (
            [
                record[k]
                for k in (
                    "physical_id",
                    *SCOPE,
                    "record_offset",
                    "record_metadata_offset",
                    "payload_length",
                    "record_timestamp_cocoa",
                    "event_key",
                    "bundle_id",
                    "event_at",
                )
            ]
            + ["start" if record["in_foreground"] else "end"]
            + [
                record[k]
                for k in (
                    "transition_reason",
                    "kind",
                    "app_version",
                    "app_build",
                    "platform_flag",
                    "parser_version",
                    "unknown_field_count",
                    "segment_filename",
                    "observed_at",
                    "object_key",
                )
            ]
            + [record["physical_id"] in segments[scope]["current"], True]
        )
        connection.execute(
            "INSERT OR REPLACE INTO ops.screen_time_record VALUES ("
            + ",".join("?" for _ in row)
            + ")",
            row,
        )
    for record in tombstones.values():
        row = [
            record[k]
            for k in (
                "physical_id",
                *SCOPE,
                "target_segment_name",
                "target_offset",
                "target_length",
                "target_event_timestamp",
                "deletion_reason",
                "observed_at",
                "object_key",
            )
        ] + [True, "unmatched"]
        connection.execute(
            "INSERT OR REPLACE INTO ops.screen_time_tombstone VALUES ("
            + ",".join("?" for _ in row)
            + ")",
            row,
        )

    # Verify the exported checkpoint's event state against the stopped warehouse.
    # Observation provenance can intentionally lag a same-content re-observation.
    from .event_state import ANALYTICAL_COLUMNS

    columns = (*ANALYTICAL_COLUMNS, "is_active")
    expected = {}
    for key, document in checkpoint.execute("SELECT event_key, document FROM events"):
        record = json.loads(document)
        record["event_at"] = datetime.fromisoformat(record["event_at"])
        expected[key] = tuple(record[k] for k in columns)
    for row in connection.execute(
        "SELECT " + ",".join(columns) + " FROM base.screen_time_event"
    ).fetchall():
        if expected.get(row[0]) != row:
            raise RuntimeError("Screen Time checkpoint events differ from warehouse")

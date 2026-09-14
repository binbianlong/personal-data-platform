"""Change-only observation state and Screen Time event resolution in local SQLite."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC

from .models import ParsedScreenTimeRecord

PROVENANCE = {"object_key", "segment_sha256", "observed_at", "segment_filename", "original_payload"}
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


def encode(value) -> str:
    return json.dumps(value, default=lambda v: v.isoformat(), sort_keys=True, separators=(",", ":"))


class EventState:
    def __init__(self, payload: bytes | None = None) -> None:
        self.db = sqlite3.connect(":memory:")
        if payload is not None:
            self.db.deserialize(payload)
            if (
                self.get("format") != 1
                or self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            ):
                raise RuntimeError("unsupported or corrupt Screen Time checkpoint")
        else:
            self.db.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE observations (
                    object_key TEXT PRIMARY KEY, scope TEXT NOT NULL, ordering TEXT NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE INDEX observation_order ON observations(scope, ordering);
                CREATE TABLE record_values (id TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE changes (
                    object_key TEXT NOT NULL, offset INTEGER NOT NULL, record_id TEXT,
                    PRIMARY KEY(object_key, offset)
                );
                CREATE TABLE events (event_key TEXT PRIMARY KEY, document TEXT NOT NULL);
            """)
            self.set("format", 1)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key = ?", [key]).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", [key, encode(value)])

    def serialize(self) -> bytes:
        self.db.commit()
        return self.db.serialize()

    def _snapshots(self, scope=None, *, changes_only=False):
        rows = self.db.execute(
            "SELECT object_key, scope, document FROM observations "
            + ("WHERE scope = ? " if scope is not None else "")
            + "ORDER BY scope, ordering",
            [scope] if scope is not None else [],
        ).fetchall()
        changes = defaultdict(list)
        for key, offset, value in self.db.execute(
            "SELECT changes.object_key, offset, record_id FROM changes "
            "JOIN observations USING (object_key) "
            + ("WHERE scope = ?" if scope is not None else ""),
            [scope] if scope is not None else [],
        ):
            changes[key].append((offset, value))
        state = {}
        previous_scope = None
        for index, (key, current_scope, document) in enumerate(rows):
            if previous_scope != current_scope:
                state = {}
                previous_scope = current_scope
            for offset, value in changes[key]:
                if value is None:
                    state.pop(offset, None)
                else:
                    state[offset] = value
            if changes_only and index + 1 < len(rows):
                next_key, next_scope, _ = rows[index + 1]
                if next_scope == current_scope and not changes[next_key]:
                    continue
            yield json.loads(document), state.copy()

    def observe(self, observation: dict, records: list[ParsedScreenTimeRecord]) -> None:
        scope = encode([observation[k] for k in ("device_key", "source_stream", "segment_key")])
        ordering = observation["observed_at"] + "\x1f" + observation["object_key"]
        before = {}
        successor = None
        successor_records = None
        for existing, values in self._snapshots(scope):
            other_order = existing["observed_at"] + "\x1f" + existing["object_key"]
            if other_order < ordering:
                before = values
            elif other_order > ordering:
                successor, successor_records = existing["object_key"], values
                break
        desired = {}
        for record in records:
            document = asdict(record)
            document["payload_length"] = record.payload_length or len(record.original_payload)
            for field in PROVENANCE:
                document.pop(field)
            content = encode(document)
            identity = hashlib.sha256(content.encode()).hexdigest()
            self.db.execute(
                "INSERT OR IGNORE INTO record_values VALUES (?, ?)", [identity, content]
            )
            if record.record_metadata_offset in desired:
                raise ValueError("duplicate metadata position in Screen Time observation")
            desired[record.record_metadata_offset] = identity
        key = observation["object_key"]
        self.db.execute(
            "INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?)",
            [key, scope, ordering, encode(observation)],
        )
        self._diff(key, before, desired)
        if successor is not None:
            self._diff(successor, desired, successor_records)
        self.db.execute(
            "DELETE FROM record_values WHERE id NOT IN "
            "(SELECT record_id FROM changes WHERE record_id IS NOT NULL)"
        )

    def _diff(self, key, before, after):
        self.db.execute("DELETE FROM changes WHERE object_key = ?", [key])
        self.db.executemany(
            "INSERT INTO changes VALUES (?, ?, ?)",
            [
                (key, offset, after.get(offset))
                for offset in before.keys() | after.keys()
                if before.get(offset) != after.get(offset)
            ],
        )

    def resolve(self) -> dict[str, dict]:
        values = {
            key: json.loads(document)
            for key, document in self.db.execute("SELECT id, document FROM record_values")
        }
        names = defaultdict(set)
        current = {}
        history = {}
        tombstones = {}
        for (document,) in self.db.execute("SELECT document FROM observations"):
            observation = json.loads(document)
            scope = tuple(observation[k] for k in ("device_key", "source_stream", "segment_key"))
            if observation.get("segment_kind") == "events" and observation.get(
                "source_segment_name"
            ):
                names[scope].add(observation["source_segment_name"])
        for observation, snapshot in self._snapshots(changes_only=True):
            scope = tuple(observation[k] for k in ("device_key", "source_stream", "segment_key"))
            if observation.get("segment_kind") == "events" and observation.get(
                "source_segment_name"
            ):
                names[scope].add(observation["source_segment_name"])
            latest = {}
            for metadata_offset, identity in snapshot.items():
                record = values[identity]
                offset = record["record_offset"]
                if offset not in latest or metadata_offset > latest[offset][0]:
                    latest[offset] = metadata_offset, identity
            candidates = []
            for _, identity in latest.values():
                record = {
                    **values[identity],
                    **{
                        k: observation[k] for k in ("object_key", "observed_at", "segment_filename")
                    },
                }
                if record["record_state"].upper() != "WRITTEN" or record["crc_passed"] is False:
                    continue
                if record["record_kind"] == "event" and record["event_key"] is not None:
                    candidates.append(record)
                    # One representative per distinct physical record version, not per observation.
                    history[identity] = record
                elif record["record_kind"] == "tombstone":
                    tombstones[identity] = record
            current[scope] = candidates
        name_scopes = defaultdict(list)
        for scope, found in names.items():
            if len(found) == 1:
                name_scopes[(*scope[:2], next(iter(found)))].append(scope)
        event_locations = defaultdict(list)
        for event in history.values():
            event_locations[
                (
                    event["device_key"],
                    event["source_stream"],
                    event["segment_key"],
                    event["record_metadata_offset"],
                    event["payload_length"],
                )
            ].append(event)
        ttl, deleted = [], set()
        diagnostics = defaultdict(int)
        for tombstone in tombstones.values():
            reason = tombstone["deletion_reason"]
            scopes = name_scopes[
                (
                    tombstone["device_key"],
                    tombstone["source_stream"],
                    tombstone["target_segment_name"],
                )
            ]
            matched = []
            if len(scopes) == 1:
                for event in event_locations[
                    (*scopes[0], tombstone["target_offset"], tombstone["target_length"])
                ]:
                    timestamp = event["record_timestamp_cocoa"]
                    target = tombstone["target_event_timestamp"]
                    if (
                        timestamp is not None
                        and target is not None
                        and abs(timestamp - target) <= 0.000001
                    ):
                        matched.append(event)
            status = (
                "unsupported_reason"
                if reason not in (1, 2)
                else "unmatched"
                if not matched
                else "ttl_history_retained"
                if reason == 1
                else "user_deletion_applied"
            )
            diagnostics[status] += 1
            if reason == 1:
                ttl.extend(matched)
            elif reason == 2:
                deleted.update(event["event_key"] for event in matched)
        retained = {}
        for event in [r for records in current.values() for r in records] + ttl:
            if event["event_key"] in deleted:
                continue
            identity = tuple(
                event[k]
                for k in (
                    "device_key",
                    "source_stream",
                    "segment_key",
                    "record_offset",
                    "event_key",
                )
            )
            if identity not in retained or self._rank(event) > self._rank(retained[identity]):
                retained[identity] = event
        grouped = defaultdict(list)
        for event in retained.values():
            grouped[event["event_key"]].append(event)
        result = {}
        for key, copies in grouped.items():
            winner = max(copies, key=self._rank)
            result[key] = {
                **{
                    column: winner[column]
                    for column in EVENT_COLUMNS
                    if column not in {"platform", "state", "duplicate_occurrence_count"}
                },
                "platform": "ios",
                "state": "start" if winner["in_foreground"] else "end",
                "duplicate_occurrence_count": len(copies) - 1,
                "is_active": True,
            }
        self.set("diagnostics", dict(diagnostics))
        return result

    @staticmethod
    def _rank(record):
        return record["observed_at"], record["object_key"], record["record_metadata_offset"]

    def updates(self, resolved: dict[str, dict]) -> list[dict]:
        previous = {
            key: json.loads(document)
            for key, document in self.db.execute("SELECT event_key, document FROM events")
        }
        updates = []
        for key in previous.keys() | resolved.keys():
            old = previous.get(key)
            new = resolved.get(key) or {**old, "is_active": False}
            if old is None or any(old[k] != new[k] for k in (*ANALYTICAL_COLUMNS, "is_active")):
                updates.append(new)
                self.db.execute("INSERT OR REPLACE INTO events VALUES (?, ?)", [key, encode(new)])
        return updates

    def seed_events(self):
        self.updates(self.resolve())


def observation_for(raw, batch) -> dict:
    return {
        "object_key": raw.key,
        "device_key": raw.subject_key,
        "source_stream": raw.stream,
        "segment_key": raw.logical_key,
        "observed_at": raw.observed_at.astimezone(UTC).isoformat(),
        "source_segment_name": batch.source_segment_name,
        "segment_kind": batch.segment_kind,
        "segment_filename": batch.source_segment_name
        or (batch.records[0].segment_filename if batch.records else raw.logical_key),
    }


def record_from_legacy(row: dict) -> ParsedScreenTimeRecord:
    row = {k: v for k, v in row.items() if k in ParsedScreenTimeRecord.__dataclass_fields__}
    return ParsedScreenTimeRecord(**row)

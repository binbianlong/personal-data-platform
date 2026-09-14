"""Coordinate a durable ingestion checkpoint with idempotent warehouse updates."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from personal_data_platform.raw.models import RawObject

from .checkpoint import FileCheckpointStore, MemoryCheckpointStore
from .event_state import EVENT_COLUMNS, EventState, observation_for, record_from_legacy

LOGGER = logging.getLogger(__name__)


class CheckpointError(RuntimeError):
    """Stop the run: an uncertain durable update must be recovered before more input."""


@dataclass
class EventBatch:
    updates: list[dict]
    parser_version: str
    record_count: int
    state_id: str
    revision: int

    def write(self, connection, raw, *, byte_size, loaded_at):
        current = connection.execute(
            "SELECT state_id, revision FROM ops.screen_time_checkpoint WHERE singleton"
        ).fetchone()
        if current != (self.state_id, self.revision - 1):
            raise CheckpointError("Screen Time checkpoint revision changed")
        columns = (*EVENT_COLUMNS, "is_active", "loaded_at")
        if self.updates:
            connection.executemany(
                f"INSERT INTO base.screen_time_event ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)}) "
                "ON CONFLICT (event_key) DO UPDATE SET "
                + ", ".join(f"{name} = excluded.{name}" for name in columns if name != "event_key"),
                [
                    [row[name] for name in (*EVENT_COLUMNS, "is_active")] + [loaded_at]
                    for row in self.updates
                ],
            )
        connection.execute(
            "UPDATE ops.screen_time_checkpoint SET revision = ? WHERE singleton", [self.revision]
        )


class ScreenTimeIngestion:
    def __init__(self, warehouse, store):
        self.warehouse = warehouse
        self.store = store
        self.failed = False
        try:
            payload = store.read()
            self.state = EventState(payload)
            marker = self._marker()
            if payload is None:
                if marker is not None or warehouse.query_value(
                    "SELECT count(*) FROM base.screen_time_event"
                ):
                    raise CheckpointError(
                        "Screen Time checkpoint is missing; restore it before loading"
                    )
                self._bootstrap()
                self.state.set("state_id", str(uuid.uuid4()))
                self.state.set("revision", 0)
                self.save()
            if marker is None:
                if self.state.get("revision") != 0 or self.state.get("pending") is not None:
                    raise CheckpointError(
                        "Screen Time checkpoint does not belong to this warehouse"
                    )
                warehouse.connection.execute(
                    "INSERT INTO ops.screen_time_checkpoint VALUES (true, ?, 0)",
                    [self.state.get("state_id")],
                )
            elif marker[0] != self.state.get("state_id"):
                raise CheckpointError("Screen Time checkpoint identity differs from warehouse")
            self.recover()
        except Exception as error:
            self.failed = True
            raise CheckpointError("cannot open Screen Time ingestion checkpoint") from error

    def _marker(self):
        return self.warehouse.connection.execute(
            "SELECT state_id, revision FROM ops.screen_time_checkpoint WHERE singleton"
        ).fetchone()

    def _bootstrap(self):
        connection = self.warehouse.connection
        observations = connection.execute(
            "SELECT * FROM base.screen_time_segment_observation ORDER BY observed_at, object_key"
        )
        names = [column[0] for column in observations.description]
        rows = observations.fetchall()
        for row in rows:
            observation = dict(zip(names, row, strict=True))
            records = connection.execute(
                "SELECT * EXCLUDE (original_payload), ''::BLOB AS original_payload "
                "FROM base.screen_time_record_occurrence WHERE object_key = ?",
                [observation["object_key"]],
            )
            columns = [column[0] for column in records.description]
            parsed = [
                record_from_legacy(dict(zip(columns, value, strict=True)))
                for value in records.fetchall()
            ]
            observation["observed_at"] = observation["observed_at"].astimezone(UTC).isoformat()
            observation["segment_filename"] = observation["source_segment_name"] or (
                parsed[0].segment_filename if parsed else observation["segment_key"]
            )
            self.state.observe(observation, parsed)
        self.state.seed_events()

    def save(self):
        self.store.write(self.state.serialize())

    def resume(self):
        if self.failed:
            raise CheckpointError("reopen the checkpoint after a failed Screen Time update")
        try:
            return self.recover()
        except Exception as error:
            self.failed = True
            raise CheckpointError("Screen Time recovery interrupted") from error

    def recover(self):
        pending = self.state.get("pending")
        marker = self._marker()
        revision = self.state.get("revision")
        if marker is None or marker[0] != self.state.get("state_id"):
            raise CheckpointError("Screen Time checkpoint identity differs from warehouse")
        if pending is None:
            if marker != (self.state.get("state_id"), revision):
                raise CheckpointError("Screen Time checkpoint revision differs from warehouse")
            return 0
        if marker[1] not in (revision - 1, revision):
            raise CheckpointError("Screen Time checkpoint has an invalid pending revision")
        raw_fields = dict(pending["raw"])
        for key in ("observed_at", "storage_created_at"):
            raw_fields[key] = datetime.fromisoformat(raw_fields[key])
        raw = RawObject(**raw_fields)
        result = 0
        if marker[1] == revision - 1:
            batch = EventBatch(
                pending["updates"],
                pending["parser_version"],
                pending["record_count"],
                self.state.get("state_id"),
                revision,
            )
            result = self.warehouse._load_object(
                raw,
                byte_size=pending["byte_size"],
                batch=batch,
                legacy_scope=(raw.subject_key, raw.logical_key),
            )
            if self._marker()[1] != revision:
                raise CheckpointError("pending Screen Time update was skipped unexpectedly")
        self.state.set("pending", None)
        self.save()
        return result

    def load(self, raw, *, byte_size, batch):
        if self.failed:
            raise CheckpointError("reopen the checkpoint after a failed Screen Time update")
        try:
            self.recover()
            current = self.warehouse._existing_object(raw)
            if (
                current
                and current[0] == "succeeded"
                and current[2] is None
                and current[3] == raw.storage_generation
                and current[10] == batch.parser_version
            ):
                return 0
            self.state.observe(observation_for(raw, batch), list(batch.records))
            updates = self.state.updates(self.state.resolve())
            self.state.set("revision", self.state.get("revision") + 1)
            self.state.set(
                "pending",
                {
                    "raw": json.loads(json.dumps(asdict(raw), default=lambda v: v.isoformat())),
                    "byte_size": byte_size,
                    "parser_version": batch.parser_version,
                    "record_count": batch.record_count,
                    "updates": updates,
                },
            )
            self.save()
            result = self.recover()
            LOGGER.info(
                "Screen Time event updates=%d deletion_status=%s",
                len(updates),
                self.state.get("diagnostics"),
            )
            return result
        except Exception as error:
            self.failed = True
            raise CheckpointError("Screen Time update interrupted; recovery required") from error

    def close(self):
        self.state.db.close()


def local_checkpoint_store(warehouse):
    row = warehouse.connection.execute(
        "SELECT path FROM duckdb_databases() WHERE database_name = current_database()"
    ).fetchone()
    if row is None:
        raise CheckpointError("no local checkpoint store for remote warehouse")
    if row[0] is None:
        return MemoryCheckpointStore()
    path = str(row[0])
    if path.startswith("md:"):
        raise CheckpointError("remote Screen Time ingestion requires a GCS checkpoint")
    return FileCheckpointStore(Path(path + ".screen-time.sqlite"))

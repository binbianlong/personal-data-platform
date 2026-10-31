"""Crash-safe local Screen Time Raw and warehouse loading state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from personal_data_platform.biome.raw import (
    ScreenTimeRawIdentity,
    format_observed_at,
    gzip_raw_bytes,
    parse_observed_at,
    sha256_hex,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS segment_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_key TEXT NOT NULL,
    stream TEXT NOT NULL,
    segment_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    compressed_payload BLOB,
    status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded')),
    uploaded_at TEXT,
    storage_created_at TEXT,
    storage_generation INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS segment_observation_latest
    ON segment_observation (device_key, stream, segment_key, id DESC);
CREATE INDEX IF NOT EXISTS segment_observation_pending
    ON segment_observation (status, id);
CREATE TABLE IF NOT EXISTS collector_stream_scan (
    stream TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL,
    device_keys TEXT NOT NULL,
    device_scanned_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class PendingObservation:
    """A durable observation awaiting warehouse loading and its gzip payload."""

    identity: ScreenTimeRawIdentity
    compressed_payload: bytes
    created: bool = False


@dataclass(frozen=True, slots=True)
class CollectorScan:
    completed_at: datetime
    device_keys: tuple[str, ...]
    device_scanned_at: dict[str, datetime]


class CollectorState:
    """SQLite-backed Raw archive and warehouse acknowledgement state."""

    def __init__(self, path: Path, *, source_id: str) -> None:
        self.source_id = source_id
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def prepare(
        self,
        *,
        device_key: str,
        stream: str,
        segment_key: str,
        raw_bytes: bytes,
        observed_at: datetime,
    ) -> PendingObservation | None:
        """Archive a new observation, or reuse a consecutive pending/loaded duplicate."""
        format_observed_at(observed_at)
        content_sha256 = sha256_hex(raw_bytes)
        compressed_payload = gzip_raw_bytes(raw_bytes)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                """
                SELECT observed_at, sha256, object_key, compressed_payload, status
                FROM segment_observation
                WHERE device_key = ? AND stream = ? AND segment_key = ?
                ORDER BY observed_at DESC, object_key DESC
                LIMIT 1
                """,
                (device_key, stream, segment_key),
            ).fetchone()
            if latest is not None and latest[1] == content_sha256:
                if latest[4] == "uploaded":
                    connection.commit()
                    return None
                payload = latest[3]
                if payload is None:
                    raise RuntimeError("pending observation has no durable payload")
                pending = PendingObservation(
                    identity=_identity_from_row(
                        source_id=self.source_id,
                        device_key=device_key,
                        stream=stream,
                        segment_key=segment_key,
                        observed_at=latest[0],
                        sha256=latest[1],
                    ),
                    compressed_payload=bytes(payload),
                    created=False,
                )
                if pending.identity.object_key != latest[2]:
                    raise RuntimeError("collector state contains an inconsistent object key")
                connection.commit()
                return pending

            effective_observed_at = observed_at.astimezone(UTC)
            if latest is not None:
                previous_observed_at = parse_observed_at(latest[0])
                if effective_observed_at <= previous_observed_at:
                    effective_observed_at = previous_observed_at + timedelta(microseconds=1)
            identity = ScreenTimeRawIdentity(
                source_id=self.source_id,
                device_key=device_key,
                stream=stream,
                segment_key=segment_key,
                observed_at=effective_observed_at,
                sha256=content_sha256,
            )
            connection.execute(
                """
                INSERT INTO segment_observation (
                    device_key, stream, segment_key, observed_at, sha256,
                    object_key, compressed_payload, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    identity.device_key,
                    identity.stream,
                    identity.segment_key,
                    format_observed_at(identity.observed_at),
                    identity.sha256,
                    identity.object_key,
                    compressed_payload,
                ),
            )
            connection.commit()
            return PendingObservation(
                identity=identity,
                compressed_payload=compressed_payload,
                created=True,
            )

    def pending(self, *, stream: str | None = None) -> list[PendingObservation]:
        """Return observations awaiting warehouse loading in their creation order."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT device_key, stream, segment_key, observed_at, sha256,
                       object_key, compressed_payload
                FROM segment_observation
                WHERE status = 'pending' AND (? IS NULL OR stream = ?)
                ORDER BY observed_at, object_key
                """,
                (stream, stream),
            ).fetchall()
        pending: list[PendingObservation] = []
        for row in rows:
            if row[6] is None:
                raise RuntimeError("pending observation has no durable payload")
            identity = _identity_from_row(
                source_id=self.source_id,
                device_key=row[0],
                stream=row[1],
                segment_key=row[2],
                observed_at=row[3],
                sha256=row[4],
            )
            if identity.object_key != row[5]:
                raise RuntimeError("collector state contains an inconsistent object key")
            pending.append(
                PendingObservation(
                    identity=identity,
                    compressed_payload=bytes(row[6]),
                    created=False,
                )
            )
        return pending

    def mark_loaded(self, object_key: str, loaded_at: datetime) -> None:
        """Retain the latest warehouse-confirmed Raw and all unconfirmed observations."""
        timestamp = format_observed_at(loaded_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE segment_observation
                SET status = 'uploaded', uploaded_at = ?
                WHERE object_key = ?
                """,
                (timestamp, object_key),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError(f"pending observation not found: {object_key}")
            connection.execute(
                """DELETE FROM segment_observation
                WHERE status = 'uploaded' AND (device_key, stream, segment_key) = (
                    SELECT device_key, stream, segment_key FROM segment_observation WHERE object_key = ?
                ) AND (observed_at, object_key) < (
                    SELECT observed_at, object_key FROM segment_observation WHERE status = 'uploaded'
                    AND (device_key, stream, segment_key) = (
                        SELECT device_key, stream, segment_key FROM segment_observation WHERE object_key = ?
                    )
                    ORDER BY observed_at DESC, object_key DESC LIMIT 1
                )""",
                (object_key, object_key),
            )
            connection.commit()

    def record_successful_scan(self, scan: CollectorScan, *, stream: str) -> None:
        """Record a completed stream scan and retain each active device's latest scan."""
        timestamp = format_observed_at(scan.completed_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT device_scanned_at FROM collector_stream_scan WHERE stream = ?", (stream,)
            ).fetchone()
            device_times = json.loads(previous[0]) if previous else {}
            device_times.update(
                {key: format_observed_at(value) for key, value in scan.device_scanned_at.items()}
            )
            device_times = {
                key: value for key, value in device_times.items() if key in scan.device_keys
            }
            connection.execute(
                """INSERT INTO collector_stream_scan VALUES (?, ?, ?, ?)
                ON CONFLICT (stream) DO UPDATE SET
                    completed_at = excluded.completed_at,
                    device_keys = excluded.device_keys,
                    device_scanned_at = excluded.device_scanned_at""",
                (stream, timestamp, json.dumps(scan.device_keys), json.dumps(device_times)),
            )

    def last_successful_scan(self, *, stream: str) -> CollectorScan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT completed_at, device_keys, device_scanned_at "
                "FROM collector_stream_scan WHERE stream = ?",
                (stream,),
            ).fetchone()
        if row is None:
            return None
        return CollectorScan(
            parse_observed_at(row[0]),
            tuple(json.loads(row[1])),
            {key: parse_observed_at(value) for key, value in json.loads(row[2]).items()},
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


def _identity_from_row(
    *,
    source_id: str,
    device_key: str,
    stream: str,
    segment_key: str,
    observed_at: str,
    sha256: str,
) -> ScreenTimeRawIdentity:
    return ScreenTimeRawIdentity(
        source_id=source_id,
        device_key=device_key,
        stream=stream,
        segment_key=segment_key,
        observed_at=parse_observed_at(observed_at),
        sha256=sha256,
    )

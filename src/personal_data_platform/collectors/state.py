"""Crash-safe local state for Screen Time Raw uploads."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from personal_data_platform.raw.screen_time import (
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
    uploaded_at TEXT
);
CREATE INDEX IF NOT EXISTS segment_observation_latest
    ON segment_observation (device_key, stream, segment_key, id DESC);
CREATE INDEX IF NOT EXISTS segment_observation_pending
    ON segment_observation (status, id);
CREATE TABLE IF NOT EXISTS collector_scan (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    completed_at TEXT NOT NULL,
    device_count INTEGER NOT NULL,
    segment_count INTEGER NOT NULL,
    uploaded_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class PendingObservation:
    """A durable upload intent and its deterministic gzip payload."""

    identity: ScreenTimeRawIdentity
    compressed_payload: bytes
    created: bool = False


@dataclass(frozen=True, slots=True)
class SuccessfulScan:
    completed_at: datetime
    device_count: int
    segment_count: int
    uploaded_count: int
    skipped_count: int


class CollectorState:
    """SQLite-backed pending-to-uploaded state machine."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        """Persist an upload intent, or skip a consecutive uploaded duplicate."""
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
                ORDER BY id DESC
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

    def pending(self) -> list[PendingObservation]:
        """Return durable upload intents in their original creation order."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT device_key, stream, segment_key, observed_at, sha256,
                       object_key, compressed_payload
                FROM segment_observation
                WHERE status = 'pending'
                ORDER BY id
                """
            ).fetchall()
        pending: list[PendingObservation] = []
        for row in rows:
            if row[6] is None:
                raise RuntimeError("pending observation has no durable payload")
            identity = _identity_from_row(
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

    def mark_uploaded(self, object_key: str, uploaded_at: datetime) -> None:
        """Atomically commit upload success and discard the staged local payload."""
        timestamp = format_observed_at(uploaded_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE segment_observation
                SET status = 'uploaded', uploaded_at = ?, compressed_payload = NULL
                WHERE object_key = ? AND status = 'pending'
                """,
                (timestamp, object_key),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError(f"pending observation not found: {object_key}")
            connection.commit()

    def record_successful_scan(self, scan: SuccessfulScan) -> None:
        """Persist liveness only after all Raw uploads and B2 receipts succeeded."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO collector_scan VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT (singleton) DO UPDATE SET
                    completed_at = excluded.completed_at,
                    device_count = excluded.device_count,
                    segment_count = excluded.segment_count,
                    uploaded_count = excluded.uploaded_count,
                    skipped_count = excluded.skipped_count
                """,
                (
                    format_observed_at(scan.completed_at),
                    scan.device_count,
                    scan.segment_count,
                    scan.uploaded_count,
                    scan.skipped_count,
                ),
            )

    def last_successful_scan(self) -> SuccessfulScan | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT completed_at, device_count, segment_count, uploaded_count, skipped_count
                FROM collector_scan WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            return None
        return SuccessfulScan(
            completed_at=parse_observed_at(row[0]),
            device_count=row[1],
            segment_count=row[2],
            uploaded_count=row[3],
            skipped_count=row[4],
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


def _identity_from_row(
    *,
    device_key: str,
    stream: str,
    segment_key: str,
    observed_at: str,
    sha256: str,
) -> ScreenTimeRawIdentity:
    return ScreenTimeRawIdentity(
        device_key=device_key,
        stream=stream,
        segment_key=segment_key,
        observed_at=parse_observed_at(observed_at),
        sha256=sha256,
    )

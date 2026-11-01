"""Durable pending payloads and compact Screen Time acknowledgement state."""

from __future__ import annotations

import fcntl
import gzip
import os
import sqlite3
import tempfile
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .raw import (
    ScreenTimeRawIdentity,
    format_observed_at,
    gzip_raw_bytes,
    parse_raw_object_key,
    sha256_hex,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_state (
    snapshot_key TEXT PRIMARY KEY,
    segment_key TEXT NOT NULL,
    device_key TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    parser_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent')),
    payload_path TEXT
);
CREATE INDEX IF NOT EXISTS file_state_segment ON file_state(segment_key, observed_at);
CREATE TABLE IF NOT EXISTS event_reference (
    segment_key TEXT NOT NULL,
    event_key TEXT NOT NULL,
    PRIMARY KEY (segment_key, event_key)
);
"""


@dataclass(frozen=True, slots=True)
class PendingObservation:
    identity: ScreenTimeRawIdentity
    compressed_payload: bytes
    created: bool = False
    parser_version: str | None = None


class CollectorState:
    """Keep all pending snapshots and only the latest sent metadata per segment."""

    def __init__(self, path: Path, *, source_id: str) -> None:
        self.source_id = source_id
        self.path = path.resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self.payload_directory = self.path.with_suffix(".payloads")
        with self._connect() as connection:
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and tables != {"file_state", "event_reference"}:
                raise RuntimeError(
                    "collector SQLite requires explicit migration; existing state retained"
                )
            connection.executescript(_SCHEMA)
        self.payload_directory.mkdir(mode=0o700, exist_ok=True)
        self.payload_directory.chmod(0o700)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialize collection and drain across processes sharing this source state."""
        fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Screen Time collector already running") from error
            yield
        finally:
            os.close(fd)

    def prepare(
        self,
        *,
        device_key: str,
        stream: str,
        segment_key: str,
        raw_bytes: bytes,
        observed_at: datetime,
        parser_version: str | None = None,
    ) -> PendingObservation | None:
        format_observed_at(observed_at)
        digest = sha256_hex(raw_bytes)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT * FROM file_state WHERE segment_key = ? ORDER BY observed_at DESC, snapshot_key DESC LIMIT 1",
                (segment_key,),
            ).fetchone()
            if latest is not None and latest[3] == digest:
                if latest[6] == "pending":
                    return self._pending_from_row(latest)
                if latest[5] == parser_version:
                    return None
            effective = observed_at.astimezone(UTC)
            previous = connection.execute("SELECT max(observed_at) FROM file_state").fetchone()[0]
            if previous is not None and effective <= datetime.fromisoformat(previous):
                effective = datetime.fromisoformat(previous) + timedelta(microseconds=1)
            identity = ScreenTimeRawIdentity(
                self.source_id, device_key, stream, segment_key, effective, digest
            )
            compressed = gzip_raw_bytes(raw_bytes)
            payload_path = self.payload_directory / (
                sha256_hex(identity.object_key.encode()) + ".segb.gz"
            )
            # The rename and directory fsync precede SQLite's durable pending row.
            fd, temporary = tempfile.mkstemp(dir=self.payload_directory)
            try:
                with os.fdopen(fd, "wb") as payload:
                    payload.write(compressed)
                    payload.flush()
                    os.fsync(payload.fileno())
                os.replace(temporary, payload_path)
                _sync_directory(self.payload_directory)
            finally:
                Path(temporary).unlink(missing_ok=True)
            connection.execute(
                "INSERT INTO file_state VALUES (?, ?, ?, ?, ?, NULL, 'pending', ?)",
                (
                    identity.object_key,
                    segment_key,
                    device_key,
                    digest,
                    effective.isoformat(),
                    str(payload_path),
                ),
            )
            connection.commit()
        return PendingObservation(identity, compressed, created=True)

    def pending(self, *, stream: str | None = None) -> list[PendingObservation]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM file_state WHERE status = 'pending' ORDER BY observed_at, snapshot_key"
            ).fetchall()
        values = [self._pending_from_row(row) for row in rows]
        return [value for value in values if stream is None or value.identity.stream == stream]

    def _pending_from_row(self, row: sqlite3.Row | tuple[object, ...]) -> PendingObservation:
        key, segment, device, digest, observed, parser, _, path = row
        raw = parse_raw_object_key(
            str(key),
            source_id=self.source_id,
            storage_created_at=datetime.fromisoformat(str(observed)),
            storage_generation=1,
        )
        if (raw.logical_key, raw.subject_key, raw.sha256, raw.observed_at) != (
            segment,
            device,
            digest,
            datetime.fromisoformat(str(observed)),
        ):
            raise RuntimeError("collector state contains an inconsistent snapshot key")
        try:
            if path is None:
                raise OSError("missing path")
            compressed = Path(str(path)).read_bytes()
            payload = gzip.decompress(compressed)
        except (OSError, EOFError, ValueError, zlib.error) as error:
            raise RuntimeError("pending payload unavailable or corrupt") from error
        if sha256_hex(payload) != digest:
            raise RuntimeError("pending payload hash mismatch")
        identity = ScreenTimeRawIdentity(
            self.source_id,
            raw.subject_key,
            raw.stream,
            raw.logical_key,
            raw.observed_at,
            raw.sha256,
        )
        return PendingObservation(
            identity, compressed, parser_version=str(parser) if parser is not None else None
        )

    def pin_parser(self, snapshot_key: str, parser_version: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT parser_version FROM file_state WHERE snapshot_key = ? AND status = 'pending'",
                (snapshot_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("pending snapshot not found")
            if row[0] is not None and row[0] != parser_version:
                raise RuntimeError("pending parser version requires compatible replay")
            connection.execute(
                "UPDATE file_state SET parser_version = ? WHERE snapshot_key = ?",
                (parser_version, snapshot_key),
            )

    def invalidated_event_keys(self, snapshot_key: str, event_keys: Sequence[str]) -> list[str]:
        """Only a same-content parser correction supplies negative evidence."""
        with self._connect() as connection:
            current = connection.execute(
                "SELECT segment_key, content_sha256, parser_version FROM file_state WHERE snapshot_key = ? AND status = 'pending'",
                (snapshot_key,),
            ).fetchone()
            if current is None or current[2] is None:
                raise RuntimeError("pending parser must be pinned")
            segment, digest, parser = current
            previous = connection.execute(
                "SELECT content_sha256, parser_version FROM file_state WHERE segment_key = ? AND status = 'sent'",
                (segment,),
            ).fetchone()
            if (
                previous is None
                or previous[0] != digest
                or previous[1] is None
                or previous[1] == parser
            ):
                return []
            unshared = connection.execute(
                "SELECT event_key FROM event_reference r WHERE segment_key = ? AND NOT EXISTS "
                "(SELECT 1 FROM event_reference other WHERE other.event_key = r.event_key AND other.segment_key <> r.segment_key) ORDER BY event_key",
                (segment,),
            ).fetchall()
        adopted = set(event_keys)
        return [key for (key,) in unshared if key not in adopted]

    def mark_loaded(self, snapshot_key: str, *, event_keys: Sequence[str]) -> None:
        """Atomically ACK and adopt references after the remote commit is confirmed."""
        # Reclaim older sent bytes before pruning their only deletion evidence.
        self.cleanup_sent_payloads()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            oldest = connection.execute(
                "SELECT snapshot_key, segment_key, parser_version FROM file_state WHERE status = 'pending' ORDER BY observed_at, snapshot_key LIMIT 1"
            ).fetchone()
            if oldest is None or oldest[0] != snapshot_key:
                raise RuntimeError("only the oldest pending snapshot may be acknowledged")
            if oldest[2] is None:
                raise RuntimeError("pending parser must be pinned")
            segment = oldest[1]
            connection.execute("DELETE FROM event_reference WHERE segment_key = ?", (segment,))
            connection.executemany(
                "INSERT INTO event_reference VALUES (?, ?)",
                [(segment, key) for key in sorted(set(event_keys))],
            )
            connection.execute(
                "DELETE FROM file_state WHERE segment_key = ? AND status = 'sent'", (segment,)
            )
            connection.execute(
                "UPDATE file_state SET status = 'sent' WHERE snapshot_key = ?", (snapshot_key,)
            )
            connection.commit()
        self.cleanup_sent_payloads()

    def cleanup_sent_payloads(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT snapshot_key, payload_path FROM file_state WHERE status = 'sent' AND payload_path IS NOT NULL"
            ).fetchall()
            for key, path in rows:
                Path(path).unlink(missing_ok=True)
                _sync_directory(Path(path).parent)
                connection.execute(
                    "UPDATE file_state SET payload_path = NULL WHERE snapshot_key = ? AND status = 'sent'",
                    (key,),
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

"""Screen Time Raw and collector controls in local SQLite."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawCodec

from .raw import (
    CollectorDeviceManifest,
    CollectorScanReceipt,
    parse_observed_at,
    parse_raw_object_key,
    scan_manifest_key,
    scan_receipt_prefix,
)
from .state import CollectorState


class ScreenTimeLocalRepository:
    """Read immutable observations and collector controls from the existing SQLite state."""

    def __init__(self, *, state: CollectorState, source: RawCodec) -> None:
        self.state = state
        self._source = source

    @classmethod
    def from_env(cls, *, source: RawCodec) -> ScreenTimeLocalRepository:
        path = Path(
            os.environ.get(
                "PDP_COLLECTOR_STATE_DB_PATH",
                str(
                    Path.home() / "Library/Application Support/personal-data-platform/collector.db"
                ),
            )
        ).expanduser()
        if not path.is_file():
            raise ValueError(f"Screen Time Raw SQLite does not exist: {path}")
        return cls(state=CollectorState(path), source=source)

    def list_raw(self, prefix: str) -> Iterable[RawObject]:
        with self.state._connect() as connection:
            rows = connection.execute(
                """SELECT object_key, coalesce(storage_created_at, observed_at),
                          storage_generation, retention_started_at
                FROM segment_observation WHERE stream = ? AND compressed_payload IS NOT NULL
                ORDER BY observed_at, object_key""",
                (self._source.stream,),
            ).fetchall()
        for key, created_at, generation, retention_at in rows:
            if key.startswith(prefix):
                raw = parse_raw_object_key(
                    key,
                    storage_created_at=parse_observed_at(created_at),
                    storage_generation=generation,
                )
                yield (
                    replace(raw, retention_started_at=parse_observed_at(retention_at))
                    if retention_at
                    else raw
                )

    def get_raw(self, key: str, *, generation: int) -> bytes:
        with self.state._connect() as connection:
            row = connection.execute(
                "SELECT storage_generation, compressed_payload FROM segment_observation WHERE object_key = ? AND stream = ?",
                (key, self._source.stream),
            ).fetchone()
        if row is None or row[1] is None:
            raise ValueError(f"Raw observation is unavailable: {key}")
        if row[0] != generation:
            raise ValueError(f"Raw generation mismatch: {key}")
        return bytes(row[1])

    def put_compressed_raw(self, key: str, compressed_bytes: bytes) -> None:
        with self.state._connect() as connection:
            row = connection.execute(
                "SELECT compressed_payload FROM segment_observation WHERE object_key = ?", (key,)
            ).fetchone()
        if row is None or row[0] != compressed_bytes:
            raise ValueError("Raw must be durably staged before collection completes")

    def put_scan_receipt(self, receipt: CollectorScanReceipt) -> None:
        self._put_control(receipt.key, receipt.to_bytes())

    def put_device_manifest(self, manifest: CollectorDeviceManifest) -> None:
        self._put_control(manifest.key, manifest.to_bytes())

    def _put_control(self, key: str, body: bytes) -> None:
        with self.state._connect() as connection:
            connection.execute(
                "INSERT INTO collector_control_object VALUES (?, ?) ON CONFLICT(object_key) DO UPDATE SET body = excluded.body",
                (key, body),
            )

    def list_scan_receipts(self) -> list[CollectorScanReceipt]:
        with self.state._connect() as connection:
            rows = connection.execute(
                "SELECT object_key, body FROM collector_control_object"
            ).fetchall()
        prefix = scan_receipt_prefix(self._source.stream) + "/"
        return [
            CollectorScanReceipt.from_bytes(key, bytes(body))
            for key, body in rows
            if key.startswith(prefix)
        ]

    def get_device_manifest(self) -> CollectorDeviceManifest | None:
        key = scan_manifest_key(self._source.stream)
        with self.state._connect() as connection:
            row = connection.execute(
                "SELECT body FROM collector_control_object WHERE object_key = ?", (key,)
            ).fetchone()
        return (
            CollectorDeviceManifest.from_bytes(bytes(row[0]), stream=self._source.stream)
            if row
            else None
        )

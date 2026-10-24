"""Screen Time Raw and collector controls in local SQLite."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawCodec

from .raw import (
    parse_observed_at,
    parse_raw_object_key,
)
from .state import CollectorScan, CollectorState


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
                          storage_generation
                FROM segment_observation WHERE stream = ? AND compressed_payload IS NOT NULL
                ORDER BY observed_at, object_key""",
                (self._source.stream,),
            ).fetchall()
        for key, created_at, generation in rows:
            if key.startswith(prefix):
                yield parse_raw_object_key(
                    key,
                    storage_created_at=parse_observed_at(created_at),
                    storage_generation=generation,
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

    def last_successful_scan(self) -> CollectorScan | None:
        return self.state.last_successful_scan(stream=self._source.stream)

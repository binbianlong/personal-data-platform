"""Immutable compressed complete-acquisition envelopes."""

from __future__ import annotations

import gzip
import hashlib
import re
from datetime import UTC, datetime
from typing import Protocol

from personal_data_platform.raw.models import RawObject

from .models import DATA_TYPES, Snapshot

PREFIX = "raw/fitbit/v1/"
_KEY = re.compile(
    r"raw/fitbit/v1/([A-Za-z0-9_-]{1,128})/health/([a-z-]+)/"
    r"(\d{8}T\d{12}Z)/([0-9a-f]{64})\.json\.gz"
)


class SnapshotRepository(Protocol):
    def put_raw_object(self, key: str, compressed_bytes: bytes) -> RawObject: ...
    def head_raw(self, key: str) -> RawObject | None: ...


def parse_raw_key(key: str, *, storage_created_at: datetime, storage_generation: int) -> RawObject:
    match = _KEY.fullmatch(key)
    if match is None or match[2] not in DATA_TYPES:
        raise ValueError("invalid Fitbit Raw key")
    observed = datetime.strptime(match[3], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    return RawObject(
        key,
        "fitbit",
        1,
        match[1],
        "health",
        match[2],
        observed,
        match[4],
        storage_created_at,
        storage_generation,
    )


def encode_snapshot(snapshot: Snapshot) -> tuple[str, bytes]:
    if snapshot.origin != "api":
        raise ValueError("Raw snapshots must originate from the API")
    payload = snapshot.to_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    timestamp = snapshot.fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
    key = f"{PREFIX}{snapshot.subject_key}/health/{snapshot.window.data_type}/{timestamp}/{digest}.json.gz"
    return key, gzip.compress(payload, mtime=0)


def save_snapshot(repository: SnapshotRepository, snapshot: Snapshot) -> RawObject:
    key, compressed = encode_snapshot(snapshot)
    return repository.put_raw_object(key, compressed)

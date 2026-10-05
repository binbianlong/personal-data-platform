"""Immutable compressed complete-acquisition envelopes."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Protocol

from personal_data_platform.raw.models import RawObject

from .models import (
    DATA_TYPES,
    CapturedSnapshot,
    FitbitBundle,
    HeartRateMinuteSnapshot,
    Snapshot,
    object_dict,
)

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


BUNDLE_PREFIX = "raw/fitbit/v3/"
MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
_BUNDLE_KEY = re.compile(
    r"raw/fitbit/v3/([A-Za-z0-9_-]{1,128})/health/(\d{8}T\d{12}Z)/([A-Za-z0-9_-]{1,128})/(\d+)/([0-9a-f]{64})\.json\.gz"
)


def parse_bundle_key(
    key: str, *, storage_created_at: datetime, storage_generation: int
) -> RawObject:
    match = _BUNDLE_KEY.fullmatch(key)
    if match is None:
        raise ValueError("invalid Fitbit complete-acquisition key")
    observed = datetime.strptime(match[2], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    return RawObject(
        key,
        "fitbit",
        3,
        match[1],
        "health",
        f"{match[3]}:{match[4]}",
        observed,
        match[5],
        storage_created_at,
        storage_generation,
    )


def encode_bundle(bundle: FitbitBundle) -> tuple[tuple[str, bytes], ...]:
    """Pack whole acquisitions; every gzip object is independently restorable."""
    groups: list[list[CapturedSnapshot | HeartRateMinuteSnapshot]] = []
    current: list[CapturedSnapshot | HeartRateMinuteSnapshot] = []

    def payload(entries: list[CapturedSnapshot | HeartRateMinuteSnapshot]) -> bytes:
        return json.dumps(
            [
                {
                    "kind": "minute" if isinstance(entry, HeartRateMinuteSnapshot) else "captured",
                    "acquisition": json.loads(entry.to_bytes()),
                }
                for entry in entries
            ],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

    for entry in bundle.entries:
        if entry.origin != "api":
            raise ValueError("Raw acquisitions must originate from the API")
        if len(gzip.compress(payload([entry]), mtime=0)) > MAX_COMPRESSED_BYTES:
            raise ValueError(
                "single acquisition exceeds compressed limit; use a narrower sync range"
            )
        if (
            current
            and len(gzip.compress(payload([*current, entry]), mtime=0)) > MAX_COMPRESSED_BYTES
        ):
            groups.append(current)
            current = []
        current.append(entry)
    groups.append(current)
    objects = []
    for index, entries in enumerate(groups):
        body = payload(entries)
        observed = max(entry.fetched_at for entry in entries).strftime("%Y%m%dT%H%M%S%fZ")
        key = f"{BUNDLE_PREFIX}{entries[0].subject_key}/health/{observed}/{bundle.bundle_id}/{index}/{hashlib.sha256(body).hexdigest()}.json.gz"
        objects.append((key, gzip.compress(body, mtime=0)))
    return tuple(objects)


def decode_bundle(payload: bytes) -> tuple[CapturedSnapshot | HeartRateMinuteSnapshot, ...]:
    rows = json.loads(payload)
    if not isinstance(rows, list) or not rows:
        raise ValueError("Raw must contain a nonempty acquisition array")
    entries: list[CapturedSnapshot | HeartRateMinuteSnapshot] = []
    for value in rows:
        row = object_dict(value)
        encoded = json.dumps(row["acquisition"], separators=(",", ":")).encode()
        if row["kind"] == "minute":
            entries.append(HeartRateMinuteSnapshot.from_bytes(encoded))
        elif row["kind"] == "captured":
            entry = CapturedSnapshot.from_bytes(encoded)
            if entry.origin != "api":
                raise ValueError("Raw acquisitions must originate from the API")
            entries.append(entry)
        else:
            raise ValueError("unsupported acquisition kind")
    FitbitBundle("decoded", tuple(entries))
    return tuple(entries)

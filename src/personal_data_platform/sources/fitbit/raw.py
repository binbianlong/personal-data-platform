"""Immutable compressed complete-acquisition envelopes."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from personal_data_platform.raw.models import RawObject

from .models import DATA_TYPES, Snapshot, object_dict, parse_time, string

PREFIX = "raw/fitbit/v1/"
BUNDLE_PREFIX = "raw/fitbit/v2/"
_KEY = re.compile(
    r"raw/fitbit/v([12])/([A-Za-z0-9_-]{1,128})/health/([a-z-]+)/"
    r"(\d{8}T\d{12}Z)/([0-9a-f]{64})\.json\.gz"
)


class SnapshotRepository(Protocol):
    def put_raw_object(self, key: str, compressed_bytes: bytes) -> RawObject: ...
    def head_raw(self, key: str) -> RawObject | None: ...


def parse_raw_key(key: str, *, storage_created_at: datetime, storage_generation: int) -> RawObject:
    match = _KEY.fullmatch(key)
    if match is None or (
        (match[1] == "1" and match[3] not in DATA_TYPES)
        or (match[1] == "2" and match[3] != "batch")
    ):
        raise ValueError("invalid Fitbit Raw key")
    observed = datetime.strptime(match[4], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    return RawObject(
        key,
        "fitbit",
        int(match[1]),
        match[2],
        "health",
        match[3],
        observed,
        match[5],
        storage_created_at,
        storage_generation,
    )


@dataclass(frozen=True, slots=True)
class SnapshotBundle:
    """Complete, non-overlapping acquisitions sharing one immutable object."""

    snapshots: tuple[Snapshot, ...]

    def __post_init__(self) -> None:
        if not self.snapshots:
            raise ValueError("Fitbit bundle must not be empty")
        if any(snapshot.subject_key != self.subject_key for snapshot in self.snapshots):
            raise ValueError("Fitbit bundle must have one subject")
        for kind in DATA_TYPES:
            windows = sorted(
                (
                    snapshot.window
                    for snapshot in self.snapshots
                    if snapshot.window.data_type == kind
                ),
                key=lambda window: window.start,
            )
            if any(left.end > right.start for left, right in zip(windows, windows[1:])):
                raise ValueError("Fitbit bundle windows must not overlap")

    @property
    def subject_key(self) -> str:
        return self.snapshots[0].subject_key

    @property
    def fetched_at(self) -> datetime:
        return max(snapshot.fetched_at for snapshot in self.snapshots)

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": 2,
                "subject_key": self.subject_key,
                "fetched_at": self.fetched_at.isoformat(),
                "snapshots": [json.loads(snapshot.to_bytes()) for snapshot in self.snapshots],
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

    @classmethod
    def from_bytes(cls, payload: bytes) -> SnapshotBundle:
        data = object_dict(json.loads(payload))
        if type(data.get("schema_version")) is not int or data["schema_version"] != 2:
            raise ValueError("unsupported Fitbit bundle schema")
        values = data.get("snapshots")
        if not isinstance(values, list):
            raise ValueError("Fitbit bundle snapshots must be an array")
        bundle = cls(tuple(Snapshot.from_bytes(json.dumps(value).encode()) for value in values))
        if (string(data["subject_key"]), parse_time(string(data["fetched_at"]))) != (
            bundle.subject_key,
            bundle.fetched_at,
        ):
            raise ValueError("Fitbit bundle identity does not match its snapshots")
        return bundle


def encode_bundle(bundle: SnapshotBundle) -> tuple[str, bytes]:
    payload = bundle.to_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    timestamp = bundle.fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
    key = f"{BUNDLE_PREFIX}{bundle.subject_key}/health/batch/{timestamp}/{digest}.json.gz"
    return key, gzip.compress(payload, mtime=0)


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

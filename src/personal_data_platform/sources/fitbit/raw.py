"""Immutable compressed complete-acquisition envelopes."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Protocol

from personal_data_platform.raw.models import RawObject

from .models import (
    DATA_TYPES,
    AcquisitionScope,
    BundleEntry,
    CapturedSnapshot,
    FitbitBundle,
    HeartRateMinuteSnapshot,
    Snapshot,
    Window,
    object_dict,
    parse_time,
    string,
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


BUNDLE_PREFIX = "raw/fitbit/v2/"
MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
_BUNDLE_KEY = re.compile(
    r"raw/fitbit/v2/([A-Za-z0-9_-]{1,128})/health/(\d{8}T\d{12}Z)/([A-Za-z0-9_-]{1,128})/(\d+)-(\d+)/([0-9a-f]{64})\.json\.gz"
)


def parse_bundle_key(
    key: str, *, storage_created_at: datetime, storage_generation: int
) -> RawObject:
    match = _BUNDLE_KEY.fullmatch(key)
    if match is None or not 0 <= int(match[4]) < int(match[5]):
        raise ValueError("invalid Fitbit bundle chunk key")
    observed = datetime.strptime(match[2], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    return RawObject(
        key,
        "fitbit",
        2,
        match[1],
        "health",
        f"{match[3]}:{int(match[4])}:{int(match[5])}",
        observed,
        match[6],
        storage_created_at,
        storage_generation,
    )


def encode_bundle(bundle: FitbitBundle) -> tuple[tuple[str, bytes], ...]:
    """Split the complete envelope so even a single large scope fits the object cap."""
    entries = []
    for entry in bundle.entries:
        entries.append(
            {
                "attempt_id": entry.attempt_id,
                "kind": "minute"
                if isinstance(entry.acquisition, HeartRateMinuteSnapshot)
                else "captured",
                "acquisition": json.loads(entry.acquisition.to_bytes()),
                "requested_scope": asdict(entry.scope),
            }
        )
    payload = json.dumps(
        {"bundle_id": bundle.bundle_id, "entries": entries},
        default=lambda value: value.isoformat(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    subject = bundle.entries[0].acquisition.subject_key
    observed = max(entry.acquisition.fetched_at for entry in bundle.entries)
    timestamp = observed.strftime("%Y%m%dT%H%M%S%fZ")
    fragment_size = min(8 * 1024 * 1024, MAX_COMPRESSED_BYTES // 2)
    while fragment_size >= 1:
        fragments = [
            payload[index : index + fragment_size]
            for index in range(0, len(payload), fragment_size)
        ]
        chunks = []
        for index, fragment in enumerate(fragments):
            wrapper = json.dumps(
                {
                    "schema_version": 2,
                    "bundle_id": bundle.bundle_id,
                    "subject_key": subject,
                    "observed_at": observed.isoformat(),
                    "chunk_index": index,
                    "chunk_count": len(fragments),
                    "payload": base64.b64encode(fragment).decode(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            compressed = gzip.compress(wrapper, mtime=0)
            key = f"{BUNDLE_PREFIX}{subject}/health/{timestamp}/{bundle.bundle_id}/{index}-{len(fragments)}/{hashlib.sha256(wrapper).hexdigest()}.json.gz"
            chunks.append((key, compressed))
        if all(len(compressed) <= MAX_COMPRESSED_BYTES for _, compressed in chunks):
            return tuple(chunks)
        fragment_size //= 2
    raise ValueError("bundle chunk envelope exceeds compressed limit")


def decode_bundle(payloads: tuple[bytes, ...]) -> FitbitBundle:
    wrappers = [object_dict(json.loads(payload)) for payload in payloads]
    if not wrappers:
        raise ValueError("bundle has no chunks")
    first = wrappers[0]
    identity = tuple(
        first.get(key)
        for key in ("schema_version", "bundle_id", "subject_key", "observed_at", "chunk_count")
    )
    count = first["chunk_count"]
    if type(count) is not int or count < 1 or first.get("schema_version") != 2:
        raise ValueError("invalid bundle chunk schema")
    indices = [row.get("chunk_index") for row in wrappers]
    if (
        len(wrappers) != count
        or any(type(index) is not int for index in indices)
        or set(indices) != set(range(count))
    ):
        raise ValueError("bundle chunks are missing or duplicated")
    if any(
        tuple(
            row.get(key)
            for key in ("schema_version", "bundle_id", "subject_key", "observed_at", "chunk_count")
        )
        != identity
        for row in wrappers
    ):
        raise ValueError("bundle chunk identity changed")
    payload = b"".join(
        base64.b64decode(string(row["payload"]), validate=True)
        for row in sorted(wrappers, key=lambda row: int(str(row["chunk_index"])))
    )
    data = object_dict(json.loads(payload))
    rows = data["entries"]
    if not isinstance(rows, list) or data["bundle_id"] != first["bundle_id"]:
        raise ValueError("bundle chunks have inconsistent body")
    entries = []
    for row in (object_dict(value) for value in rows):
        encoded = json.dumps(row["acquisition"], separators=(",", ":")).encode()
        kind = row["kind"]
        if kind == "minute":
            acquisition: CapturedSnapshot | HeartRateMinuteSnapshot = (
                HeartRateMinuteSnapshot.from_bytes(encoded)
            )
        elif kind == "captured":
            acquisition = CapturedSnapshot.from_bytes(encoded)
        else:
            raise ValueError("unsupported bundle acquisition kind")
        scope = object_dict(row["requested_scope"])
        window = object_dict(scope["window"])
        requested = AcquisitionScope(
            string(scope["subject_key"]),
            Window(
                string(window["data_type"]),
                parse_time(string(window["start"])),
                parse_time(string(window["end"])),
            ),
            string(scope["aggregation_version"]),
        )
        entries.append(BundleEntry(string(row["attempt_id"]), acquisition, requested))
    bundle = FitbitBundle(string(data["bundle_id"]), tuple(entries))
    if bundle.entries[0].acquisition.subject_key != first["subject_key"] or max(
        entry.acquisition.fetched_at for entry in bundle.entries
    ) != parse_time(string(first["observed_at"])):
        raise ValueError("bundle chunk metadata does not match acquisition")
    return bundle

"""Durable, generation-guarded Fitbit scheduling progress outside receipt retention."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Protocol

from .api import SyncTime

CONTROL_PREFIX = "raw/fitbit/v1/_control/device-sync/"
_SUBJECT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_WEEK = re.compile(r"\d{4}-W\d{2}\Z")


@dataclass(frozen=True, slots=True)
class SyncState:
    subject_key: str
    bootstrap_day: date | None = None
    bootstrap_complete: bool = False
    last_completed_sync: SyncTime | None = None
    device_target: SyncTime | None = None
    device_next_day: date | None = None
    device_batch_end: date | None = None
    weekly_completed: str | None = None
    weekly_pending: str | None = None
    weekly_end_day: date | None = None

    def __post_init__(self) -> None:
        if _SUBJECT.fullmatch(self.subject_key) is None:
            raise ValueError("invalid sync subject")
        if self.bootstrap_complete and (
            self.bootstrap_day is None or self.last_completed_sync is None
        ):
            raise ValueError("completed bootstrap needs a day and sync cursor")
        if self.device_target is None and (
            self.device_next_day is not None or self.device_batch_end is not None
        ):
            raise ValueError("device progress needs a target")
        if self.device_target is not None and self.device_next_day is None:
            raise ValueError("device target needs a next day")
        if self.device_batch_end is not None and (
            self.device_next_day is None or self.device_batch_end < self.device_next_day
        ):
            raise ValueError("invalid device batch")
        for week in (self.weekly_completed, self.weekly_pending):
            if week is not None and _WEEK.fullmatch(week) is None:
                raise ValueError("invalid ISO week")
        if (self.weekly_pending is None) != (self.weekly_end_day is None):
            raise ValueError("weekly progress needs a fixed end day")

    def to_bytes(self) -> bytes:
        values = asdict(self)
        for key in ("bootstrap_day", "device_next_day", "device_batch_end", "weekly_end_day"):
            value = getattr(self, key)
            values[key] = value.isoformat() if value is not None else None
        for key in ("last_completed_sync", "device_target"):
            value = getattr(self, key)
            values[key] = value.text if value is not None else None
        return json.dumps(
            {"schema_version": 1, **values}, sort_keys=True, separators=(",", ":")
        ).encode()

    @classmethod
    def from_bytes(cls, payload: bytes) -> SyncState:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                raise ValueError("unsupported sync control schema")
            expected = set(cls.__dataclass_fields__) | {"schema_version"}
            if set(data) != expected:
                raise ValueError("unsupported sync control schema")
            for key in ("bootstrap_day", "device_next_day", "device_batch_end", "weekly_end_day"):
                if data[key] is not None:
                    data[key] = date.fromisoformat(data[key])
            for key in ("last_completed_sync", "device_target"):
                if data[key] is not None:
                    data[key] = SyncTime.parse(data[key])
            del data["schema_version"]
            return cls(**data)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid sync control schema") from error


@dataclass(frozen=True, slots=True)
class StoredSyncState:
    state: SyncState
    generation: int


class _Blob(Protocol):
    generation: int | None
    metadata: dict[str, str] | None

    def upload_from_string(
        self, data: bytes, *, content_type: str, if_generation_match: int
    ) -> None: ...
    def download_as_bytes(self, *, raw_download: bool, if_generation_match: int) -> bytes: ...


class _Bucket(Protocol):
    def blob(self, name: str) -> _Blob: ...
    def get_blob(self, name: str) -> _Blob | None: ...


class _Client(Protocol):
    def bucket(self, name: str) -> _Bucket: ...


class GCSFitbitSyncState:
    def __init__(self, *, client: _Client, bucket: str) -> None:
        self._bucket = client.bucket(bucket)

    @staticmethod
    def _key(subject_key: str) -> str:
        if _SUBJECT.fullmatch(subject_key) is None:
            raise ValueError("invalid sync subject")
        return f"{CONTROL_PREFIX}{subject_key}.json"

    def read(self, subject_key: str) -> StoredSyncState:
        blob = self._bucket.get_blob(self._key(subject_key))
        if blob is None:
            return StoredSyncState(SyncState(subject_key), 0)
        if blob.generation is None:
            raise RuntimeError("sync control omitted its generation")
        generation = int(blob.generation)
        state = SyncState.from_bytes(
            blob.download_as_bytes(raw_download=True, if_generation_match=generation)
        )
        if state.subject_key != subject_key:
            raise ValueError("sync control belongs to another subject")
        return StoredSyncState(state, generation)

    def replace(self, stored: StoredSyncState, state: SyncState) -> StoredSyncState:
        if state.subject_key != stored.state.subject_key:
            raise ValueError("sync control subject cannot change")
        blob = self._bucket.blob(self._key(state.subject_key))
        blob.metadata = {}
        blob.upload_from_string(
            state.to_bytes(), content_type="application/json", if_generation_match=stored.generation
        )
        if blob.generation is None:
            raise RuntimeError("sync control upload omitted its generation")
        return StoredSyncState(state, int(blob.generation))

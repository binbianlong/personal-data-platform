"""Durable SQLite checkpoints outside the analytical warehouse."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from google.api_core.exceptions import NotFound
from google.cloud.storage.retry import DEFAULT_RETRY_IF_GENERATION_SPECIFIED

CHECKPOINT_PREFIX = "control/screen_time/app-in-focus/"


class CheckpointStore(Protocol):
    def read(self) -> bytes | None: ...

    def write(self, payload: bytes) -> None: ...


class MemoryCheckpointStore:
    """An isolated checkpoint for a scratch rebuild or an in-memory warehouse."""

    def __init__(self) -> None:
        self.payload: bytes | None = None

    def read(self) -> bytes | None:
        return self.payload

    def write(self, payload: bytes) -> None:
        self.payload = payload


class FileCheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> bytes | None:
        return self.path.read_bytes() if self.path.exists() else None

    def write(self, payload: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".checkpoint-")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)


class GCSCheckpointStore:
    """Replace one checkpoint with generation fencing; never retry a stale writer."""

    def __init__(self, bucket, key: str) -> None:
        self.bucket = bucket
        self.key = key
        self.generation = 0

    def read(self) -> bytes | None:
        blob = self.bucket.blob(self.key)
        try:
            blob.reload()
        except NotFound:
            self.generation = 0
            return None
        self.generation = int(blob.generation)
        return blob.download_as_bytes(raw_download=True, if_generation_match=self.generation)

    def write(self, payload: bytes) -> None:
        blob = self.bucket.blob(self.key)
        blob.upload_from_string(
            payload,
            content_type="application/vnd.sqlite3",
            if_generation_match=self.generation,
            checksum="crc32c",
            retry=DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
        )
        self.generation = int(blob.generation)

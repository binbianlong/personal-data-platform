"""Collector for iPhone App.InFocus and local Mac AppUsage segments."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from personal_data_platform.biome.collector_state import (
    CollectorScan,
    CollectorState,
)
from personal_data_platform.biome.raw import (
    build_device_key,
    build_segment_key,
    encode_segment_envelope,
)
from personal_data_platform.loader.models import RawDecodeError

from .parser import validate_segb_snapshot


class CollectorSourceError(RuntimeError):
    """Raised when the local Biome source cannot be inspected safely."""


@dataclass(frozen=True, slots=True)
class BiomeDevice:
    """A selected row from Biome's DevicePeer table."""

    identifier: str
    name: str | None
    model: str | None


@dataclass(frozen=True, slots=True)
class CollectionStats:
    """One complete local scan result."""

    devices: int = 0
    segments: int = 0
    archived: int = 0
    skipped: int = 0
    deferred: int = 0


def list_segment_files(directory: Path) -> list[tuple[Path, str]]:
    """List regular SEGB paths under one Biome stream without following symlinks."""
    segments: list[tuple[Path, str]] = []
    directories = [directory]
    try:
        while directories:
            with os.scandir(directories.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        path = Path(entry.path)
                        segments.append((path, path.relative_to(directory).as_posix()))
    except OSError as error:
        raise CollectorSourceError("failed to enumerate Screen Time segments") from error
    return sorted(segments, key=lambda item: item[1])


def select_completed_segments(segments: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """Treat a numeric segment as complete only when its directory has a newer one."""
    newest: dict[Path, int] = {}
    numbered: list[tuple[Path, str, int]] = []
    for path, relative_path in segments:
        if not path.name.isascii() or not path.name.isdecimal():
            raise CollectorSourceError(
                f"cannot determine completion of a non-numeric segment name: {relative_path}"
            )
        number = int(path.name)
        numbered.append((path, relative_path, number))
        newest[path.parent] = max(newest.get(path.parent, number), number)
    return [
        (path, relative_path)
        for path, relative_path, number in numbered
        if number < newest[path.parent]
    ]


class BiomeSegmentSource(Protocol):
    raw_stream: str
    segment_stream: str
    platform: str

    def list_devices(self) -> list[BiomeDevice]: ...
    def device_directory(self, device: BiomeDevice) -> Path: ...
    def list_segments(self, device: BiomeDevice) -> list[tuple[Path, str]]: ...


class ScreenTimeCollector:
    """Archive each changed segment in durable local state before warehouse loading."""

    def __init__(
        self,
        *,
        source: BiomeSegmentSource,
        state: CollectorState,
        pseudonym_key: bytes,
        allowed_device_keys: frozenset[str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._state = state
        self._pseudonym_key = pseudonym_key
        self._allowed_device_keys = allowed_device_keys
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def stream(self) -> str:
        return self._source.raw_stream

    def collect_once(self) -> CollectionStats:
        """Archive valid snapshots while retaining all observations awaiting loading."""
        if not self._allowed_device_keys:
            raise CollectorSourceError("Screen Time device allowlist is empty")
        archived = 0

        discovered_devices = self._source.list_devices()
        devices = [
            device
            for device in discovered_devices
            if build_device_key(self._pseudonym_key, device.identifier) in self._allowed_device_keys
        ]
        if not devices:
            raise CollectorSourceError(
                f"no allowlisted {self._source.platform} devices found in Biome sync.db"
            )

        missing_directories = [
            device for device in devices if not self._source.device_directory(device).is_dir()
        ]
        if missing_directories:
            raise CollectorSourceError(
                f"{self._source.segment_stream} directory is missing for "
                f"{len(missing_directories)} allowlisted device(s)"
            )

        segment_count = 0
        device_segment_counts: dict[str, int] = {}
        skipped = 0
        deferred = 0
        for device in devices:
            device_key = build_device_key(self._pseudonym_key, device.identifier)
            segments = [
                (path, relative_path)
                for path, relative_path in self._source.list_segments(device)
                if path.parent.name != "tombstone"
            ]
            select_completed_segments(segments)  # Validate the numeric names in every directory.
            segment_count += len(segments)
            for path, relative_path in segments:
                raw_bytes = _read_stable_bytes(path)
                segment_key = build_segment_key(
                    self._pseudonym_key,
                    device_identifier=device.identifier,
                    stream=self._source.segment_stream,
                    relative_path=relative_path,
                )
                observed_at = self._clock()
                envelope = encode_segment_envelope(raw_bytes)
                try:
                    validate_segb_snapshot(raw_bytes)
                except RawDecodeError:
                    deferred += 1
                    continue
                observation = self._state.prepare(
                    device_key=device_key,
                    stream=self._source.raw_stream,
                    segment_key=segment_key,
                    raw_bytes=envelope,
                    observed_at=observed_at,
                )
                if observation is None:
                    skipped += 1
                    continue
                if observation.created:
                    archived += 1
            device_segment_counts[device_key] = len(segments)

        stats = CollectionStats(
            devices=len(devices),
            segments=segment_count,
            archived=archived,
            skipped=skipped,
            deferred=deferred,
        )
        if deferred:
            return stats
        completed_at = self._clock()
        self._state.record_successful_scan(
            CollectorScan(
                completed_at,
                tuple(sorted(self._allowed_device_keys)),
                {key: completed_at for key in device_segment_counts},
            ),
            stream=self.stream,
        )
        return stats


def _validate_device_identifier(identifier: object) -> None:
    if not isinstance(identifier, str) or not identifier:
        raise CollectorSourceError("Biome device identifier must be a non-empty string")
    if identifier in {".", ".."} or "/" in identifier or "\\" in identifier or "\0" in identifier:
        raise CollectorSourceError("Biome device identifier contains an unsafe path character")


def _read_stable_bytes(path: Path, *, attempts: int = 3, delay_seconds: float = 0.05) -> bytes:
    """Avoid archiving a segment while Biome is still replacing or extending it."""
    for attempt in range(attempts):
        try:
            before = path.stat()
            value = path.read_bytes()
            after = path.stat()
        except OSError as error:
            if attempt + 1 == attempts:
                raise CollectorSourceError(f"failed to read stable segment: {path.name}") from error
            time.sleep(delay_seconds)
            continue
        identity_before = (before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before == identity_after and len(value) == after.st_size:
            return value
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    raise CollectorSourceError(f"segment remained unstable while reading: {path.name}")

"""Collector for iPhone App.InFocus segments synced to macOS Biome."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from personal_data_platform.collectors.state import (
    CollectorState,
    PendingObservation,
    SuccessfulScan,
)
from personal_data_platform.raw.screen_time import (
    APP_IN_FOCUS_STREAM,
    CollectorDeviceManifest,
    CollectorScanReceipt,
    build_device_key,
    build_segment_key,
)


class CollectorSourceError(RuntimeError):
    """Raised when the local Biome source cannot be inspected safely."""


class CompressedRawUploader(Protocol):
    """Minimal capability required by the write-only local collector."""

    def put_compressed_raw(self, key: str, compressed_bytes: bytes) -> None: ...

    def put_scan_receipt(self, receipt: CollectorScanReceipt) -> None: ...

    def put_device_manifest(self, manifest: CollectorDeviceManifest) -> None: ...


@dataclass(frozen=True, slots=True)
class IPhoneDevice:
    """An iPhone row selected from Biome's DevicePeer table."""

    identifier: str
    name: str | None
    model: str | None


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    """Pseudonymized device information safe for object paths and diagnostics."""

    device_key: str
    name: str | None
    model: str | None
    stream_directory_exists: bool
    allowed: bool


@dataclass(frozen=True, slots=True)
class CollectionStats:
    """One complete local scan result."""

    devices: int = 0
    segments: int = 0
    uploaded: int = 0
    skipped: int = 0
    retried: int = 0


class BiomeScreenTimeSource:
    """Read-only access to iPhone device and App.InFocus segment locations."""

    def __init__(self, *, sync_db_path: Path, remote_dir: Path) -> None:
        self.sync_db_path = sync_db_path
        self.remote_dir = remote_dir

    def list_iphone_devices(self) -> list[IPhoneDevice]:
        if not self.sync_db_path.is_file():
            raise CollectorSourceError(f"Biome sync database is not readable: {self.sync_db_path}")
        uri = f"file:{quote(str(self.sync_db_path), safe='/')}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                rows = connection.execute(
                    """
                    SELECT device_identifier, name, model
                    FROM DevicePeer
                    WHERE platform = 2
                    ORDER BY device_identifier
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise CollectorSourceError(f"failed to read Biome DevicePeer: {error}") from error
        devices: list[IPhoneDevice] = []
        for identifier, name, model in rows:
            _validate_device_identifier(identifier)
            devices.append(IPhoneDevice(identifier=identifier, name=name, model=model))
        return devices

    def device_directory(self, device: IPhoneDevice) -> Path:
        _validate_device_identifier(device.identifier)
        return self.remote_dir / device.identifier

    def list_segments(self, device: IPhoneDevice) -> list[tuple[Path, str]]:
        directory = self.device_directory(device)
        if not directory.is_dir():
            return []
        segments: list[tuple[Path, str]] = []
        for path in directory.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative_path = path.relative_to(directory).as_posix()
            segments.append((path, relative_path))
        return sorted(segments, key=lambda item: item[1])


class ScreenTimeCollector:
    """Persist each changed segment before uploading it with write-only credentials."""

    def __init__(
        self,
        *,
        source: BiomeScreenTimeSource,
        state: CollectorState,
        uploader: CompressedRawUploader,
        pseudonym_key: bytes,
        allowed_device_keys: frozenset[str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._state = state
        self._uploader = uploader
        self._pseudonym_key = pseudonym_key
        self._allowed_device_keys = allowed_device_keys
        self._clock = clock or (lambda: datetime.now(UTC))

    def devices(self) -> list[DeviceSummary]:
        """List iPhones without exposing their stable Apple identifiers."""
        return [
            DeviceSummary(
                device_key=build_device_key(self._pseudonym_key, device.identifier),
                name=device.name,
                model=device.model,
                stream_directory_exists=self._source.device_directory(device).is_dir(),
                allowed=(
                    build_device_key(self._pseudonym_key, device.identifier)
                    in self._allowed_device_keys
                ),
            )
            for device in self._source.list_iphone_devices()
        ]

    def collect_once(self) -> CollectionStats:
        """Retry durable pending uploads, then scan every current iPhone segment."""
        if not self._allowed_device_keys:
            raise CollectorSourceError("Screen Time device allowlist is empty")
        retried = 0
        uploaded = 0
        for observation in self._state.pending():
            # The observation was allowlisted when this durable upload intent was created.
            self._upload(observation)
            retried += 1
            uploaded += 1

        discovered_devices = self._source.list_iphone_devices()
        devices = [
            device
            for device in discovered_devices
            if build_device_key(self._pseudonym_key, device.identifier) in self._allowed_device_keys
        ]
        if not devices:
            raise CollectorSourceError(
                "no allowlisted platform=2 iPhone devices found in Biome sync.db"
            )

        missing_directories = [
            device for device in devices if not self._source.device_directory(device).is_dir()
        ]
        if missing_directories:
            raise CollectorSourceError(
                "App.InFocus directory is missing for "
                f"{len(missing_directories)} allowlisted device(s)"
            )

        segment_count = 0
        device_segment_counts: dict[str, int] = {}
        skipped = 0
        for device in devices:
            device_key = build_device_key(self._pseudonym_key, device.identifier)
            current_device_segment_count = 0
            for path, relative_path in self._source.list_segments(device):
                segment_count += 1
                current_device_segment_count += 1
                raw_bytes = _read_stable_bytes(path)
                segment_key = build_segment_key(
                    self._pseudonym_key,
                    device_identifier=device.identifier,
                    stream="App.InFocus",
                    relative_path=relative_path,
                )
                observation = self._state.prepare(
                    device_key=device_key,
                    stream=APP_IN_FOCUS_STREAM,
                    segment_key=segment_key,
                    raw_bytes=raw_bytes,
                    observed_at=self._clock(),
                )
                if observation is None:
                    skipped += 1
                    continue
                self._upload(observation)
                uploaded += 1
            device_segment_counts[device_key] = current_device_segment_count

        stats = CollectionStats(
            devices=len(devices),
            segments=segment_count,
            uploaded=uploaded,
            skipped=skipped,
            retried=retried,
        )
        completed_at = self._clock()
        for device in devices:
            device_key = build_device_key(self._pseudonym_key, device.identifier)
            self._uploader.put_scan_receipt(
                CollectorScanReceipt(
                    device_key=device_key,
                    completed_at=completed_at,
                    segment_count=device_segment_counts[device_key],
                )
            )
        self._uploader.put_device_manifest(
            CollectorDeviceManifest(
                device_keys=tuple(sorted(self._allowed_device_keys)),
                completed_at=completed_at,
            )
        )
        self._state.record_successful_scan(
            SuccessfulScan(
                completed_at=completed_at,
                device_count=stats.devices,
                segment_count=stats.segments,
                uploaded_count=stats.uploaded,
                skipped_count=stats.skipped,
            )
        )
        return stats

    def _upload(self, observation: PendingObservation) -> None:
        self._uploader.put_compressed_raw(
            observation.identity.object_key,
            observation.compressed_payload,
        )
        self._state.mark_uploaded(observation.identity.object_key, self._clock())


def _validate_device_identifier(identifier: object) -> None:
    if not isinstance(identifier, str) or not identifier:
        raise CollectorSourceError("Biome device identifier must be a non-empty string")
    if identifier in {".", ".."} or "/" in identifier or "\\" in identifier or "\0" in identifier:
        raise CollectorSourceError("Biome device identifier contains an unsafe path character")


def _read_stable_bytes(path: Path, *, attempts: int = 3, delay_seconds: float = 0.05) -> bytes:
    """Avoid uploading a segment while Biome is still replacing or extending it."""
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

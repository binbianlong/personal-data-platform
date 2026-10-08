"""Screen Time Raw uploads and collector control objects in GCS."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import google.cloud.storage as storage
from google.api_core.exceptions import NotFound

from personal_data_platform.config import GCSConfig
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawCodec
from personal_data_platform.storage.gcs import GCSRawRepository
from personal_data_platform.storage.gcs_types import GCSClient

from .raw import (
    CollectorDeviceManifest,
    CollectorScanReceipt,
    ScreenTimeRawIdentity,
    gzip_raw_bytes,
    is_scan_receipt_key,
    parse_observed_at,
    parse_raw_object_key,
    scan_manifest_key,
    scan_receipt_prefix,
    sha256_hex,
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


class ScreenTimeGCSRepository(GCSRawRepository):
    """Extend the shared transport with the Screen Time collector protocol."""

    def __init__(self, *, client: GCSClient, bucket: str, source: RawCodec | None = None) -> None:
        from personal_data_platform.sources.registry import get_source

        super().__init__(
            client=client,
            bucket=bucket,
            source=source or get_source("screen_time", "app-in-focus"),
        )

    @classmethod
    def from_config(
        cls, config: GCSConfig, *, source: RawCodec | None = None
    ) -> ScreenTimeGCSRepository:
        return cls(
            client=storage.Client(project=config.project_id), bucket=config.bucket, source=source
        )

    @classmethod
    def from_env(cls, *, source: RawCodec | None = None) -> ScreenTimeGCSRepository:
        return cls.from_config(GCSConfig.from_env(), source=source)

    def store_raw(self, identity: ScreenTimeRawIdentity, raw_bytes: bytes) -> str:
        """Validate, compress, and upload a Screen Time observation."""
        actual_sha256 = sha256_hex(raw_bytes)
        if actual_sha256 != identity.sha256:
            raise ValueError(
                f"Raw SHA-256 mismatch: identity={identity.sha256}, actual={actual_sha256}"
            )
        self.put_compressed_raw(identity.object_key, gzip_raw_bytes(raw_bytes))
        return identity.object_key

    def put_scan_receipt(self, receipt: CollectorScanReceipt) -> None:
        """Replace the fixed latest liveness receipt after a successful scan."""
        self._bucket.blob(receipt.key).upload_from_string(
            receipt.to_bytes(), content_type="application/json"
        )

    def put_device_manifest(self, manifest: CollectorDeviceManifest) -> None:
        """Replace the fixed registry of active pseudonymized devices."""
        self._bucket.blob(manifest.key).upload_from_string(
            manifest.to_bytes(), content_type="application/json"
        )

    def list_scan_receipts(self) -> list[CollectorScanReceipt]:
        """Return each current collector liveness receipt."""
        keys: set[str] = set()
        stream = self._source.stream
        iterator = self._client.list_blobs(self._bucket, prefix=f"{scan_receipt_prefix(stream)}/")
        for page in iterator.pages:
            for blob in page:
                if is_scan_receipt_key(blob.name):
                    keys.add(blob.name)
        receipts: list[CollectorScanReceipt] = []
        for key in sorted(keys):
            # Read by name so a replaced receipt is not pinned to its listed generation.
            value = self._bucket.blob(key).download_as_bytes(raw_download=True)
            receipts.append(CollectorScanReceipt.from_bytes(key, value))
        return receipts

    def get_device_manifest(self) -> CollectorDeviceManifest | None:
        """Return the current expected-device registry, or None when absent."""
        try:
            value = self._bucket.blob(scan_manifest_key(self._source.stream)).download_as_bytes(
                raw_download=True
            )
        except NotFound:
            return None
        return CollectorDeviceManifest.from_bytes(value, stream=self._source.stream)

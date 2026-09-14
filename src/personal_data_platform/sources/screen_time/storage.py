"""Screen Time Raw uploads and collector control objects in GCS."""

from __future__ import annotations

from typing import Any

from google.api_core.exceptions import NotFound
from google.cloud import storage

from personal_data_platform.config import GCSConfig
from personal_data_platform.sources.contracts import RawCodec
from personal_data_platform.storage.gcs import GCSRawRepository

from .raw import (
    SCAN_MANIFEST_KEY,
    SCAN_RECEIPT_PREFIX,
    CollectorDeviceManifest,
    CollectorScanReceipt,
    ScreenTimeRawIdentity,
    gzip_raw_bytes,
    is_scan_receipt_key,
    sha256_hex,
)


class ScreenTimeGCSRepository(GCSRawRepository):
    """Extend the shared transport with the Screen Time collector protocol."""

    def __init__(self, *, client: Any, bucket: str, source: RawCodec | None = None) -> None:
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

    def checkpoint_store(self, warehouse):
        import hashlib

        from .checkpoint import CHECKPOINT_PREFIX, GCSCheckpointStore

        database = warehouse.query_value("SELECT current_database()")
        namespace = hashlib.sha256(database.encode()).hexdigest()
        return GCSCheckpointStore(self._bucket, f"{CHECKPOINT_PREFIX}{namespace}/state.sqlite")

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
        self._bucket.blob(SCAN_MANIFEST_KEY).upload_from_string(
            manifest.to_bytes(), content_type="application/json"
        )

    def list_scan_receipts(self) -> list[CollectorScanReceipt]:
        """Return each current collector liveness receipt."""
        keys: set[str] = set()
        iterator = self._client.list_blobs(self._bucket, prefix=f"{SCAN_RECEIPT_PREFIX}/")
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
            value = self._bucket.blob(SCAN_MANIFEST_KEY).download_as_bytes(raw_download=True)
        except NotFound:
            return None
        return CollectorDeviceManifest.from_bytes(value)

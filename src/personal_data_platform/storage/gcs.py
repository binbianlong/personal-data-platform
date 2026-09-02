"""Google Cloud Storage repository for immutable Screen Time Raw objects."""

from __future__ import annotations

import base64
from typing import Any

import google_crc32c
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY_IF_GENERATION_SPECIFIED

from personal_data_platform.config import GCSConfig
from personal_data_platform.raw.screen_time import (
    RAW_PREFIX,
    SCAN_MANIFEST_KEY,
    SCAN_RECEIPT_PREFIX,
    CollectorDeviceManifest,
    CollectorScanReceipt,
    RawObservationRef,
    ScreenTimeRawIdentity,
    gzip_raw_bytes,
    is_scan_receipt_key,
    parse_raw_object_key,
    sha256_hex,
    validate_raw_object_key,
)


class GCSRawRepository:
    """Read/write adapter using Application Default Credentials."""

    def __init__(self, *, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = client.bucket(bucket)

    @classmethod
    def from_config(cls, config: GCSConfig) -> GCSRawRepository:
        """Build a GCS client using the runtime's Application Default Credentials."""
        return cls(client=storage.Client(project=config.project_id), bucket=config.bucket)

    @classmethod
    def from_env(cls) -> GCSRawRepository:
        """Build the cloud-side repository from environment and workload ADC."""
        return cls.from_config(GCSConfig.from_env())

    def put_compressed_raw(self, key: str, compressed_bytes: bytes) -> None:
        """Create immutable pre-compressed Raw without any read or list request."""
        validate_raw_object_key(key)
        blob = self._bucket.blob(key)
        blob.content_encoding = "gzip"
        crc32c_checksum_value = base64.b64encode(
            google_crc32c.Checksum(compressed_bytes).digest()
        ).decode("ascii")
        try:
            blob.upload_from_string(
                compressed_bytes,
                content_type="application/octet-stream",
                if_generation_match=0,
                checksum="crc32c",
                crc32c_checksum_value=crc32c_checksum_value,
                retry=DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
            )
        except PreconditionFailed:
            # A durable retry can race with the first completed upload. The key embeds
            # the uncompressed SHA-256 and is verified again by the loader.
            return

    def put_scan_receipt(self, receipt: CollectorScanReceipt) -> None:
        """Replace the fixed latest liveness receipt after a successful scan."""
        self._bucket.blob(receipt.key).upload_from_string(
            receipt.to_bytes(),
            content_type="application/json",
        )

    def put_device_manifest(self, manifest: CollectorDeviceManifest) -> None:
        """Replace the fixed registry of active pseudonymized devices."""
        self._bucket.blob(SCAN_MANIFEST_KEY).upload_from_string(
            manifest.to_bytes(),
            content_type="application/json",
        )

    def store_raw(self, identity: ScreenTimeRawIdentity, raw_bytes: bytes) -> str:
        """Validate, compress, and upload a Raw observation."""
        actual_sha256 = sha256_hex(raw_bytes)
        if actual_sha256 != identity.sha256:
            raise ValueError(
                f"Raw SHA-256 mismatch: identity={identity.sha256}, actual={actual_sha256}"
            )
        self.put_compressed_raw(identity.object_key, gzip_raw_bytes(raw_bytes))
        return identity.object_key

    def get_raw(self, key: str, *, generation: int) -> bytes:
        """Download the listed generation without GCS content transcoding."""
        validate_raw_object_key(key)
        return self._bucket.blob(key, generation=generation).download_as_bytes(
            raw_download=True,
            if_generation_match=generation,
        )

    def list_raw(self, prefix: str = f"{RAW_PREFIX}/") -> list[RawObservationRef]:
        """List every canonical Raw observation across all GCS result pages."""
        observations: list[RawObservationRef] = []
        iterator = self._client.list_blobs(self._bucket, prefix=prefix)
        for page in iterator.pages:
            for blob in page:
                name = getattr(blob, "name", None)
                if not isinstance(name, str):
                    raise RuntimeError("GCS listing returned a Raw object without a name")
                if not name.endswith(".segb.gz"):
                    continue
                try:
                    validate_raw_object_key(name)
                except ValueError:
                    raise RuntimeError(
                        "GCS listing contained a noncanonical .segb.gz object under Raw prefix"
                    ) from None
                storage_created_at = blob.time_created
                if storage_created_at is None:
                    raise RuntimeError(f"GCS listing omitted time_created for Raw object: {name}")
                storage_generation = blob.generation
                if storage_generation is None:
                    raise RuntimeError(f"GCS listing omitted generation for Raw object: {name}")
                observations.append(
                    parse_raw_object_key(
                        name,
                        storage_created_at=storage_created_at,
                        storage_generation=int(storage_generation),
                    )
                )
        return sorted(observations, key=lambda item: (item.observed_at, item.key))

    def list_scan_receipts(self) -> list[CollectorScanReceipt]:
        """Return each current collector liveness receipt."""
        keys: set[str] = set()
        iterator = self._client.list_blobs(
            self._bucket,
            prefix=f"{SCAN_RECEIPT_PREFIX}/",
        )
        for page in iterator.pages:
            for blob in page:
                if is_scan_receipt_key(blob.name):
                    keys.add(blob.name)

        receipts: list[CollectorScanReceipt] = []
        for key in sorted(keys):
            # Rebuild by name so the read targets the latest generation even if the
            # receipt was replaced after its listing metadata was returned.
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

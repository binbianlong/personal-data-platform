"""Google Cloud Storage transport for immutable source-owned Raw objects."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import google.cloud.storage as storage
import google_crc32c
from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage.retry import DEFAULT_RETRY_IF_GENERATION_SPECIFIED

from personal_data_platform.config import GCSConfig
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawCodec, selected_prefixes

from .gcs_types import GCSBucket, GCSClient


@dataclass(frozen=True)
class _RawMetadata:
    name: object
    time_created: datetime | None
    generation: int | None


def _list_metadata(client: GCSClient, bucket: GCSBucket, prefix: str) -> Iterator[_RawMetadata]:
    for page in client.list_blobs(bucket, prefix=prefix).pages:
        for blob in page:
            yield _RawMetadata(
                name=getattr(blob, "name", None),
                time_created=getattr(blob, "time_created", None),
                generation=getattr(blob, "generation", None),
            )


class GCSRawInventory:
    """Share complete immutable listing snapshots within one read-only job run."""

    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str], tuple[_RawMetadata, ...]] = {}

    def list_metadata(
        self, client: GCSClient, bucket: GCSBucket, bucket_name: str, prefix: str
    ) -> tuple[_RawMetadata, ...]:
        key = (bucket_name, prefix)
        if key not in self._snapshots:
            # Publish only a fully exhausted listing, never partial pagination.
            self._snapshots[key] = tuple(_list_metadata(client, bucket, prefix))
        return self._snapshots[key]

    def invalidate(self, bucket_name: str, prefixes: tuple[str, ...]) -> None:
        for prefix in prefixes:
            self._snapshots.pop((bucket_name, prefix), None)


class GCSRawRepository:
    """Read/write transport whose object contract is supplied by a source codec."""

    def __init__(self, *, client: GCSClient, bucket: str, source: RawCodec) -> None:
        self._client = client
        self._bucket = client.bucket(bucket)
        self._bucket_name = bucket
        self._source = source
        self._raw_inventory: GCSRawInventory | None = None

    def use_raw_inventory(self, inventory: GCSRawInventory) -> None:
        """Reuse a run-scoped inventory with other repositories using the same ADC."""
        self._raw_inventory = inventory

    def invalidate_raw_inventory(self) -> None:
        """Require fresh listings before checking concurrently committed warehouse keys."""
        if self._raw_inventory is not None:
            self._raw_inventory.invalidate(self._bucket_name, self._source.raw_prefixes)

    @classmethod
    def from_config(cls, config: GCSConfig, *, source: RawCodec) -> GCSRawRepository:
        """Build a GCS client using the runtime's Application Default Credentials."""
        return cls(
            client=storage.Client(project=config.project_id), bucket=config.bucket, source=source
        )

    @classmethod
    def from_env(cls, *, source: RawCodec) -> GCSRawRepository:
        """Build the cloud-side repository from environment and workload ADC."""
        return cls.from_config(GCSConfig.from_env(), source=source)

    def put_compressed_raw(self, key: str, compressed_bytes: bytes) -> None:
        """Create immutable pre-compressed Raw without any read or list request."""
        self._source.validate_raw_key(key)
        blob = self._bucket.blob(key)
        blob.content_encoding = "gzip"
        # google-crc32c ships an unannotated extension API.
        checksum = google_crc32c.Checksum(compressed_bytes)  # type: ignore[no-untyped-call]
        digest: bytes = checksum.digest()  # type: ignore[no-untyped-call]
        crc32c_checksum_value = base64.b64encode(digest).decode("ascii")
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
            # The durable retry uses the same content-derived identity. The loader
            # verifies the uncompressed checksum of the stored generation again.
            return

    def put_raw_object(self, key: str, compressed_bytes: bytes) -> RawObject:
        """Save one immutable observation and obtain its exact storage generation."""
        self.put_compressed_raw(key, compressed_bytes)
        blob = self._bucket.blob(key)
        blob.reload()
        if blob.time_created is None or blob.generation is None:
            raise RuntimeError("GCS omitted immutable object metadata")
        return self._source.parse_raw_key(
            key, storage_created_at=blob.time_created, storage_generation=int(blob.generation)
        )

    def get_raw(self, key: str, *, generation: int) -> bytes:
        """Download the listed generation without GCS content transcoding."""
        self._source.validate_raw_key(key)
        return self._bucket.blob(key, generation=generation).download_as_bytes(
            raw_download=True,
            if_generation_match=generation,
        )

    def head_raw(self, key: str) -> RawObject | None:
        """Find one immutable Raw generation without listing the namespace."""
        self._source.validate_raw_key(key)
        blob = self._bucket.get_blob(key)
        if blob is None:
            return None
        if blob.time_created is None or blob.generation is None:
            raise RuntimeError("GCS omitted immutable object metadata")
        return self._source.parse_raw_key(
            key, storage_created_at=blob.time_created, storage_generation=int(blob.generation)
        )

    def list_raw(self, prefix: str | None = None) -> list[RawObject]:
        """List all supported versions and return only the selected source stream."""
        observations: list[RawObject] = []
        seen_keys: set[str] = set()
        for selected_prefix in selected_prefixes(self._source, prefix):
            metadata = (
                _list_metadata(self._client, self._bucket, selected_prefix)
                if self._raw_inventory is None
                else self._raw_inventory.list_metadata(
                    self._client, self._bucket, self._bucket_name, selected_prefix
                )
            )
            for blob in metadata:
                name = blob.name
                if not isinstance(name, str):
                    raise RuntimeError("GCS listing returned a Raw object without a name")
                if not name.endswith(self._source.raw_suffixes):
                    continue
                try:
                    if not name.startswith(selected_prefix):
                        raise ValueError("object is outside the selected Raw prefix")
                    self._source.validate_raw_key(name)
                except ValueError:
                    raise RuntimeError(
                        "GCS listing contained a noncanonical object under Raw prefix"
                    ) from None
                storage_created_at = blob.time_created
                if storage_created_at is None:
                    raise RuntimeError(f"GCS listing omitted time_created for Raw object: {name}")
                storage_generation = blob.generation
                if storage_generation is None:
                    raise RuntimeError(f"GCS listing omitted generation for Raw object: {name}")
                raw = self._source.parse_raw_key(
                    name,
                    storage_created_at=storage_created_at,
                    storage_generation=int(storage_generation),
                )
                if raw.source_id != self._source.source_id:
                    raise RuntimeError("Raw codec returned a mismatched source")
                if raw.schema_version not in self._source.schema_versions:
                    raise RuntimeError("Raw codec returned an unsupported schema version")
                if raw.stream != self._source.stream:
                    continue
                if raw.key in seen_keys:
                    raise RuntimeError("GCS listing returned duplicate Raw object keys")
                seen_keys.add(raw.key)
                observations.append(raw)
        return sorted(observations, key=lambda item: (item.observed_at, item.key))

from __future__ import annotations

import base64
import gzip
from datetime import UTC, datetime
from typing import Any

import google_crc32c
import pytest
from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed

from personal_data_platform.sources.screen_time.raw import (
    SCAN_MANIFEST_KEY,
    CollectorDeviceManifest,
    CollectorScanReceipt,
    ScreenTimeRawIdentity,
    sha256_hex,
)
from personal_data_platform.sources.screen_time.storage import ScreenTimeGCSRepository


class _ListedBlob:
    def __init__(
        self,
        name: str,
        time_created: datetime | None,
        generation: int | None = 1,
    ) -> None:
        self.name = name
        self.time_created = time_created
        self.generation = generation


class _Iterator:
    def __init__(self, pages: list[list[_ListedBlob]]) -> None:
        self.pages = pages


class _Blob:
    def __init__(self, bucket: _Bucket, name: str, generation: int | None = None) -> None:
        self._bucket = bucket
        self.name = name
        self.generation = generation
        self.content_encoding: str | None = None

    def upload_from_string(self, data: bytes, **kwargs: Any) -> None:
        self._bucket.upload_calls.append(
            {
                "name": self.name,
                "data": data,
                "content_encoding": self.content_encoding,
                **kwargs,
            }
        )
        if self._bucket.upload_error is not None:
            raise self._bucket.upload_error
        self.generation = self._bucket.next_generation
        self._bucket.next_generation += 1
        self._bucket.objects[self.name] = data

    def download_as_bytes(self, **kwargs: Any) -> bytes:
        self._bucket.download_calls.append(
            {"name": self.name, "generation": self.generation, **kwargs}
        )
        if self.name not in self._bucket.objects:
            raise NotFound("missing")
        return self._bucket.objects[self.name]


class _Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.upload_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []
        self.blob_calls: list[tuple[str, int | None]] = []
        self.next_generation = 1
        self.upload_error: Exception | None = None

    def blob(self, name: str, generation: int | None = None) -> _Blob:
        self.blob_calls.append((name, generation))
        return _Blob(self, name, generation)


class FakeGCSClient:
    def __init__(self) -> None:
        self.bucket_ref = _Bucket()
        self.list_pages: list[list[_ListedBlob]] = []
        self.list_calls: list[dict[str, Any]] = []

    def bucket(self, name: str) -> _Bucket:
        assert name == "synthetic-bucket"
        return self.bucket_ref

    def list_blobs(self, bucket: _Bucket, **kwargs: Any) -> _Iterator:
        assert bucket is self.bucket_ref
        self.list_calls.append(kwargs)
        return _Iterator(self.list_pages)


def _identity(observed_at: datetime, marker: str = "a") -> ScreenTimeRawIdentity:
    return ScreenTimeRawIdentity(
        device_key="1" * 64,
        stream="app-in-focus",
        segment_key="2" * 64,
        observed_at=observed_at,
        sha256=marker * 64,
    )


def test_store_raw_is_create_only_and_marks_precompressed_gzip() -> None:
    client = FakeGCSClient()
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    raw_bytes = b"synthetic-segb"
    identity = ScreenTimeRawIdentity(
        device_key="1" * 64,
        stream="app-in-focus",
        segment_key="2" * 64,
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        sha256=sha256_hex(raw_bytes),
    )

    stored_key = repository.store_raw(identity, raw_bytes)

    assert stored_key == identity.object_key
    assert len(client.bucket_ref.upload_calls) == 1
    call = client.bucket_ref.upload_calls[0]
    assert gzip.decompress(call["data"]) == raw_bytes
    assert call["content_encoding"] == "gzip"
    assert call["content_type"] == "application/octet-stream"
    assert call["if_generation_match"] == 0
    assert call["checksum"] == "crc32c"
    assert call["crc32c_checksum_value"] == base64.b64encode(
        google_crc32c.Checksum(call["data"]).digest()
    ).decode("ascii")
    assert call["retry"] is not None
    assert client.list_calls == []


def test_create_precondition_failure_is_the_only_idempotent_existing_result() -> None:
    client = FakeGCSClient()
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    key = _identity(datetime(2026, 8, 27, tzinfo=UTC)).object_key
    client.bucket_ref.upload_error = PreconditionFailed("already exists")

    repository.put_compressed_raw(key, b"gzip")

    client.bucket_ref.upload_error = Forbidden("denied")
    with pytest.raises(Forbidden, match="denied"):
        repository.put_compressed_raw(key, b"gzip")


def test_get_raw_disables_gcs_content_transcoding() -> None:
    client = FakeGCSClient()
    compressed = gzip.compress(b"synthetic-segb", mtime=0)
    identity = _identity(datetime(2026, 8, 27, tzinfo=UTC))
    client.bucket_ref.objects[identity.object_key] = compressed
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")

    downloaded = repository.get_raw(identity.object_key, generation=7)

    assert downloaded == compressed
    assert client.bucket_ref.download_calls == [
        {
            "name": identity.object_key,
            "generation": 7,
            "raw_download": True,
            "if_generation_match": 7,
        }
    ]


def test_list_raw_follows_pages_ignores_other_objects_and_sorts_replay_order() -> None:
    client = FakeGCSClient()
    earlier = _identity(datetime(2026, 8, 27, 1, tzinfo=UTC), "a")
    later = _identity(datetime(2026, 8, 27, 2, tzinfo=UTC), "b")
    earlier_created = datetime(2026, 8, 28, tzinfo=UTC)
    later_created = datetime(2026, 8, 29, tzinfo=UTC)
    client.list_pages = [
        [
            _ListedBlob(later.object_key, later_created),
            _ListedBlob("diagnostics/not-raw", datetime(2026, 8, 27, tzinfo=UTC)),
        ],
        [_ListedBlob(earlier.object_key, earlier_created)],
    ]
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")

    observations = repository.list_raw()

    assert [item.key for item in observations] == [earlier.object_key, later.object_key]
    assert [item.storage_created_at for item in observations] == [earlier_created, later_created]
    assert [item.storage_generation for item in observations] == [1, 1]
    assert client.list_calls == [{"prefix": "raw/screen_time/v1/"}]


def test_list_raw_rejects_missing_gcs_creation_time() -> None:
    client = FakeGCSClient()
    identity = _identity(datetime(2026, 8, 27, tzinfo=UTC))
    client.list_pages = [[_ListedBlob(identity.object_key, None)]]
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")

    with pytest.raises(RuntimeError, match="time_created"):
        repository.list_raw()


def test_list_raw_rejects_noncanonical_segment_objects() -> None:
    client = FakeGCSClient()
    client.list_pages = [
        [
            _ListedBlob(
                "raw/screen_time/v1/unpseudonymized.segb.gz",
                datetime(2026, 8, 27, tzinfo=UTC),
            )
        ]
    ]
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")

    with pytest.raises(RuntimeError, match="noncanonical"):
        repository.list_raw()


def test_list_raw_rejects_missing_gcs_generation() -> None:
    client = FakeGCSClient()
    identity = _identity(datetime(2026, 8, 27, tzinfo=UTC))
    client.list_pages = [
        [_ListedBlob(identity.object_key, datetime(2026, 8, 27, tzinfo=UTC), None)]
    ]
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")

    with pytest.raises(RuntimeError, match="generation"):
        repository.list_raw()


def test_scan_receipt_replaces_fixed_key_and_reads_latest_blob_by_name() -> None:
    client = FakeGCSClient()
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    receipt = CollectorScanReceipt(
        device_key="1" * 64,
        completed_at=datetime(2026, 8, 27, tzinfo=UTC),
        segment_count=3,
    )

    repository.put_scan_receipt(receipt)

    call = client.bucket_ref.upload_calls[0]
    assert call["name"] == receipt.key
    assert call["content_type"] == "application/json"
    assert "if_generation_match" not in call
    assert b"synthetic" not in call["data"]

    client.list_pages = [[_ListedBlob(receipt.key, datetime(2026, 8, 27, tzinfo=UTC))]]
    assert repository.list_scan_receipts() == [receipt]
    assert client.bucket_ref.download_calls[-1] == {
        "name": receipt.key,
        "generation": None,
        "raw_download": True,
    }


def test_device_manifest_replaces_fixed_key_and_missing_is_explicit() -> None:
    client = FakeGCSClient()
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    manifest = CollectorDeviceManifest(
        device_keys=("1" * 64,),
        completed_at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert repository.get_device_manifest() is None
    repository.put_device_manifest(manifest)

    call = client.bucket_ref.upload_calls[-1]
    assert call["name"] == SCAN_MANIFEST_KEY
    assert call["content_type"] == "application/json"
    assert repository.get_device_manifest() == manifest


def test_registered_screen_time_stream_is_filtered_without_failing_selected_stream(
    monkeypatch,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from personal_data_platform.sources import registry

    monkeypatch.setitem(
        registry._SOURCE_FACTORIES,
        ("screen_time", "synthetic-other"),
        lambda: SimpleNamespace(source_id="screen_time", stream="synthetic-other"),
    )
    client = FakeGCSClient()
    selected = _identity(datetime(2026, 8, 27, tzinfo=UTC))
    other = replace(selected, stream="synthetic-other")
    client.list_pages = [
        [
            _ListedBlob(other.object_key, other.observed_at),
            _ListedBlob(selected.object_key, selected.observed_at),
        ]
    ]
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")

    assert [raw.key for raw in repository.list_raw()] == [selected.object_key]
    source = registry.get_source()
    other_raw = source.parse_raw_key(
        other.object_key, storage_created_at=other.observed_at, storage_generation=1
    )
    with pytest.raises(ValueError, match="does not match Screen Time"):
        source.decode(other_raw, b"not-decoded")


def test_gcs_rejects_unregistered_stream_and_unknown_schema() -> None:
    from dataclasses import replace

    identity = _identity(datetime(2026, 8, 27, tzinfo=UTC))
    source_keys = (
        replace(identity, stream="unregistered").object_key,
        identity.object_key.replace("/v1/", "/v2/"),
    )
    for key in source_keys:
        client = FakeGCSClient()
        client.list_pages = [[_ListedBlob(key, identity.observed_at)]]
        repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
        with pytest.raises(RuntimeError, match="noncanonical"):
            repository.list_raw()


def test_gcs_rejects_foreign_listing_prefix_before_cloud_access() -> None:
    client = FakeGCSClient()
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    with pytest.raises(ValueError, match="selected source namespace"):
        repository.list_raw("raw/another_source/v1/")
    assert client.list_calls == []

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


def test_mac_audit_requires_its_own_receipt_even_when_iphone_is_fresh() -> None:
    from personal_data_platform.sources.registry import get_source
    from personal_data_platform.sources.screen_time.audit import audit_source

    now = datetime(2026, 9, 26, tzinfo=UTC)
    client = FakeGCSClient()
    phone_repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    mac_repository = ScreenTimeGCSRepository(
        client=client,
        bucket="synthetic-bucket",
        source=get_source("screen_time", "app-usage"),
    )
    phone_receipt = CollectorScanReceipt("a" * 64, now, 1)
    mac_receipt = CollectorScanReceipt("b" * 64, now, 1, stream="app-usage")
    phone_repository.put_scan_receipt(phone_receipt)
    phone_repository.put_device_manifest(CollectorDeviceManifest(("a" * 64,), now))
    client.list_pages = [[_ListedBlob(phone_receipt.key, now)]]
    mac_source = get_source("screen_time", "app-usage")
    assert mac_source.audit(mac_repository, [], now).ok

    mac_repository.put_device_manifest(
        CollectorDeviceManifest(("b" * 64,), now, stream="app-usage")
    )

    health = audit_source(mac_repository, [], now)
    assert not health.ok and health.details["missing_collector_receipt_count"] == 1

    mac_repository.put_scan_receipt(mac_receipt)
    client.list_pages[0].append(_ListedBlob(mac_receipt.key, now))
    assert audit_source(mac_repository, [], now).ok


def test_mac_audit_distinguishes_missing_manifest_from_explicit_deactivation() -> None:
    from datetime import timedelta

    from personal_data_platform.sources.registry import get_source

    now = datetime(2026, 9, 26, tzinfo=UTC)
    client = FakeGCSClient()
    source = get_source("screen_time", "app-usage")
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket", source=source)
    receipt = CollectorScanReceipt("b" * 64, now, 1, stream="app-usage")
    repository.put_scan_receipt(receipt)
    client.list_pages = [[_ListedBlob(receipt.key, now)]]
    assert not source.audit(repository, [], now).ok

    repository.put_device_manifest(
        CollectorDeviceManifest((), now - timedelta(days=2), stream="app-usage")
    )
    health = source.audit(repository, [], now)
    assert health.ok
    assert health.details["collector_inactive"] is True


def test_iphone_requires_manifest_until_explicitly_marked_inactive() -> None:
    from datetime import timedelta

    from personal_data_platform.sources.registry import get_source

    now = datetime(2026, 9, 26, tzinfo=UTC)
    client = FakeGCSClient()
    source = get_source("screen_time", "app-in-focus")
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket", source=source)
    assert not source.audit(repository, [], now).ok

    repository.put_device_manifest(CollectorDeviceManifest((), now - timedelta(days=2)))
    health = source.audit(repository, [], now)
    assert health.ok
    assert health.details["collector_inactive"] is True


def test_mac_raw_without_manifest_is_not_treated_as_never_activated() -> None:
    from personal_data_platform.sources.registry import get_source

    now = datetime(2026, 9, 26, tzinfo=UTC)
    source = get_source("screen_time", "app-usage")
    repository = ScreenTimeGCSRepository(
        client=FakeGCSClient(), bucket="synthetic-bucket", source=source
    )
    raw_key = ScreenTimeRawIdentity(
        device_key="b" * 64,
        stream="app-usage",
        segment_key="c" * 64,
        observed_at=now,
        sha256="d" * 64,
        schema_version=2,
    ).object_key
    raw = source.parse_raw_key(raw_key, storage_created_at=now, storage_generation=1)
    assert not source.audit(repository, [raw], now).ok


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
        return _Iterator(
            [
                [blob for blob in page if blob.name.startswith(kwargs["prefix"])]
                for page in self.list_pages
            ]
        )


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


def test_put_raw_object_uses_upload_metadata_without_reload(monkeypatch) -> None:
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.models import Snapshot, Window
    from personal_data_platform.sources.fitbit.raw import encode_snapshot
    from personal_data_platform.storage.gcs import GCSRawRepository

    now = datetime(2026, 10, 2, tzinfo=UTC)
    key, compressed = encode_snapshot(
        Snapshot("self", Window("steps", now, now.replace(day=3)), now, ())
    )
    client = FakeGCSClient()
    monkeypatch.setattr(_Blob, "time_created", now, raising=False)

    def forbidden_reload(self):
        raise AssertionError("successful upload already returned metadata")

    monkeypatch.setattr(_Blob, "reload", forbidden_reload, raising=False)
    repository = GCSRawRepository(client=client, bucket="synthetic-bucket", source=FitbitSource())
    raw = repository.put_raw_object(key, compressed)
    assert raw.storage_created_at == now and raw.storage_generation == 1
    assert len(client.bucket_ref.upload_calls) == 1
    assert client.bucket_ref.download_calls == [] and client.list_calls == []


def test_duplicate_put_recovers_existing_generation_once(monkeypatch) -> None:

    now = datetime(2026, 10, 2, tzinfo=UTC)
    identity = _identity(now)
    client = FakeGCSClient()
    client.bucket_ref.upload_error = PreconditionFailed("already exists")
    monkeypatch.setattr(_Blob, "time_created", now, raising=False)
    lookups = []

    def get_blob(self, key):
        lookups.append(key)
        return _Blob(self, key, 123)

    monkeypatch.setattr(_Bucket, "get_blob", get_blob, raising=False)
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    raw = repository.put_raw_object(identity.object_key, b"compressed")
    assert raw.storage_generation == 123
    assert lookups == [identity.object_key]
    assert client.bucket_ref.download_calls == [] and client.list_calls == []


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
    assert client.list_calls == [
        {"prefix": "raw/screen_time/v1/"},
        {"prefix": "raw/screen_time/v2/"},
    ]


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
    source_keys = (replace(identity, stream="unregistered").object_key,)
    for key in source_keys:
        client = FakeGCSClient()
        client.list_pages = [[_ListedBlob(key, identity.observed_at)]]
        repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
        with pytest.raises(RuntimeError, match="noncanonical"):
            repository.list_raw()


def test_raw_codec_rejects_unknown_schema() -> None:
    from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource

    identity = _identity(datetime(2026, 8, 27, tzinfo=UTC))
    with pytest.raises(ValueError, match="invalid Screen Time"):
        ScreenTimeSource().validate_raw_key(identity.object_key.replace("/v1/", "/v3/"))


def test_gcs_rejects_foreign_listing_prefix_before_cloud_access() -> None:
    client = FakeGCSClient()
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    with pytest.raises(ValueError, match="selected source namespace"):
        repository.list_raw("raw/another_source/v1/")
    assert client.list_calls == []


def test_raw_inventory_shares_all_pages_between_streams_and_clients() -> None:
    from dataclasses import replace

    from personal_data_platform.sources.registry import get_source
    from personal_data_platform.storage.gcs import GCSRawInventory

    now = datetime(2026, 9, 26, tzinfo=UTC)
    phone = _identity(now)
    mac = replace(phone, stream="app-usage", schema_version=2)
    first_client, second_client = FakeGCSClient(), FakeGCSClient()
    first_client.list_pages = [
        [_ListedBlob(phone.object_key, now, 7)],
        [_ListedBlob(mac.object_key, now, 9)],
    ]
    inventory = GCSRawInventory()
    phone_repo = ScreenTimeGCSRepository(client=first_client, bucket="synthetic-bucket")
    mac_repo = ScreenTimeGCSRepository(
        client=second_client,
        bucket="synthetic-bucket",
        source=get_source("screen_time", "app-usage"),
    )
    for repo in (phone_repo, mac_repo):
        repo.use_raw_inventory(inventory)

    assert [(raw.key, raw.storage_generation) for raw in phone_repo.list_raw()] == [
        (phone.object_key, 7)
    ]
    assert [(raw.key, raw.storage_generation) for raw in mac_repo.list_raw()] == [
        (mac.object_key, 9)
    ]
    assert first_client.list_calls == [
        {"prefix": "raw/screen_time/v1/"},
        {"prefix": "raw/screen_time/v2/"},
    ]
    assert second_client.list_calls == []


def test_raw_inventory_keeps_generation_snapshot_until_explicit_refresh() -> None:
    from personal_data_platform.storage.gcs import GCSRawInventory

    now = datetime(2026, 9, 26, tzinfo=UTC)
    original, later = _identity(now), _identity(now, "b")
    client = FakeGCSClient()
    original_blob = _ListedBlob(original.object_key, now, 7)
    client.list_pages = [[original_blob]]
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    repository.use_raw_inventory(GCSRawInventory())
    assert [raw.storage_generation for raw in repository.list_raw()] == [7]

    original_blob.generation = 9
    client.list_pages[0].append(_ListedBlob(later.object_key, now, 11))
    assert [raw.storage_generation for raw in repository.list_raw()] == [7]
    assert len(client.list_calls) == 2

    repository.invalidate_raw_inventory()
    assert [(raw.key, raw.storage_generation) for raw in repository.list_raw()] == [
        (original.object_key, 9),
        (later.object_key, 11),
    ]
    assert len(client.list_calls) == 4


def test_failed_listing_page_is_not_reused_as_complete_inventory() -> None:
    from types import SimpleNamespace

    from personal_data_platform.storage.gcs import GCSRawInventory

    now = datetime(2026, 9, 26, tzinfo=UTC)
    first, second = _identity(now), _identity(now, "b")

    class InterruptedClient(FakeGCSClient):
        fail = True

        def list_blobs(self, bucket, **kwargs):
            iterator = super().list_blobs(bucket, **kwargs)

            def pages():
                for page in iterator.pages:
                    yield page
                    if self.fail:
                        raise RuntimeError("synthetic pagination outage")

            return SimpleNamespace(pages=pages())

    client = InterruptedClient()
    client.list_pages = [
        [_ListedBlob(first.object_key, now)],
        [_ListedBlob(second.object_key, now)],
    ]
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    repository.use_raw_inventory(GCSRawInventory())
    with pytest.raises(RuntimeError, match="pagination outage"):
        repository.list_raw()

    client.fail = False
    assert [raw.key for raw in repository.list_raw()] == [first.object_key, second.object_key]
    assert client.list_calls == [
        {"prefix": "raw/screen_time/v1/"},
        {"prefix": "raw/screen_time/v1/"},
        {"prefix": "raw/screen_time/v2/"},
    ]


def test_daily_job_repairs_both_streams_with_one_inventory_and_refreshes_next_run(
    monkeypatch,
) -> None:
    from personal_data_platform.reconciliation import job
    from personal_data_platform.sources.registry import get_sources
    from personal_data_platform.sources.screen_time.raw import scan_receipt_prefix
    from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig
    from tests.screen_time_helpers import Repository, event, mac_usage_event, segb

    now = datetime.now(UTC)
    sources = get_sources("screen_time", all_streams=True)
    client = FakeGCSClient()
    input_repository = Repository()
    phone = input_repository.add("100", segb(event("app.phone"))[0], version=1)
    mac = input_repository.add(
        "200", segb(mac_usage_event("app.mac", 12, start=True))[0], stream="app-usage"
    )
    client.list_pages = [[_ListedBlob(raw.key, now) for raw in (phone, mac)]]
    for source in sources:
        repository = ScreenTimeGCSRepository(
            client=client, bucket="synthetic-bucket", source=source
        )
        receipt = CollectorScanReceipt("a" * 64, now, 1, stream=source.stream)
        repository.put_scan_receipt(receipt)
        repository.put_device_manifest(
            CollectorDeviceManifest(("a" * 64,), now, stream=source.stream)
        )
        client.list_pages[0].append(_ListedBlob(receipt.key, now))
    client.bucket_ref.objects.update(
        {key: payload for key, (_, payload) in input_repository.objects.items()}
    )
    reports = []

    class AuditWarehouse(Warehouse):
        def migrate(self):
            super().migrate()
            required = {relation for source in sources for relation in source.required_relations}
            for relation in required - job._relation_names(self):
                self.connection.execute(f"CREATE OR REPLACE VIEW {relation} AS SELECT 1 AS value")

        def record_reconciliation(self, result):
            super().record_reconciliation(result)
            if result.status != "running":
                reports.append(
                    (result.details["stream"], result.status, result.loaded_object_count)
                )

    monkeypatch.setenv("PDP_RECONCILIATION_MONITORING_MODE", "cloud_monitoring")
    monkeypatch.setenv("PDP_FITBIT_REPAIR_ENABLED", "false")
    monkeypatch.delenv("RECONCILIATION_HEARTBEAT_URL", raising=False)
    monkeypatch.setattr(job.WarehouseConfig, "from_env", lambda: WarehouseConfig(":memory:"))
    monkeypatch.setattr(job, "Warehouse", AuditWarehouse)
    monkeypatch.setattr(
        ScreenTimeGCSRepository,
        "from_env",
        classmethod(
            lambda cls, *, source=None: cls(client=client, bucket="synthetic-bucket", source=source)
        ),
    )
    assert job.run_reconciliation_from_env(source_id="screen_time", all_streams=True) == 0
    assert reports == [("app-in-focus", "succeeded", 1), ("app-usage", "succeeded", 1)]

    later = input_repository.add("300", segb(event("app.later"))[0])
    client.list_pages[0].append(_ListedBlob(later.key, now))
    client.bucket_ref.objects[later.key] = input_repository.objects[later.key][1]
    assert job.run_reconciliation_from_env(source_id="screen_time", all_streams=True) == 0
    assert reports[-2:] == [("app-in-focus", "succeeded", 2), ("app-usage", "succeeded", 1)]
    assert (
        client.list_calls
        == [
            {"prefix": prefix}
            for prefix in (
                "raw/screen_time/v1/",
                "raw/screen_time/v2/",
                f"{scan_receipt_prefix('app-in-focus')}/",
                f"{scan_receipt_prefix('app-usage')}/",
            )
        ]
        * 2
    )
    raw_reads = [
        call for call in client.bucket_ref.download_calls if call["name"].endswith(".segb.gz")
    ]
    assert len(raw_reads) == 5
    assert all(call["generation"] == call["if_generation_match"] == 1 for call in raw_reads)


def test_reconciliation_refresh_bypasses_shared_inventory_for_concurrent_warehouse_keys() -> None:
    from personal_data_platform.reconciliation.job import _relation_names, run_reconciliation
    from personal_data_platform.sources.registry import get_source
    from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
    from personal_data_platform.storage.gcs import GCSRawInventory
    from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

    now = datetime.now(UTC)
    identity = _identity(now)
    source = get_source()
    client = FakeGCSClient()
    repository = ScreenTimeGCSRepository(client=client, bucket="synthetic-bucket")
    repository.use_raw_inventory(GCSRawInventory())
    receipt = CollectorScanReceipt(identity.device_key, now, 1)
    repository.put_scan_receipt(receipt)
    repository.put_device_manifest(CollectorDeviceManifest((identity.device_key,), now))
    client.list_pages = [[_ListedBlob(receipt.key, now)]]

    class ConcurrentWarehouse(Warehouse):
        def active_ingestion_states(self, **kwargs):
            if not any(blob.name == identity.object_key for blob in client.list_pages[0]):
                client.list_pages[0].append(_ListedBlob(identity.object_key, now, 7))
                raw = source.parse_raw_key(
                    identity.object_key, storage_created_at=now, storage_generation=7
                )
                self.load_object(raw, byte_size=0, batch=ScreenTimeBatch([]))
            return super().active_ingestion_states(**kwargs)

    warehouse = ConcurrentWarehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate()
        for relation in set(source.required_relations) - _relation_names(warehouse):
            warehouse.connection.execute(f"CREATE OR REPLACE VIEW {relation} AS SELECT 1 AS value")
        result = run_reconciliation(repository, warehouse, heartbeat=lambda _: None, now=now)
        assert result.ok
        assert result.loaded_object_count == result.raw_object_count == 1
        assert result.orphaned_loaded_object_count == 0
        assert client.list_calls == [
            {"prefix": "raw/screen_time/v1/"},
            {"prefix": "raw/screen_time/v2/"},
            {"prefix": "raw/screen_time/v1/"},
            {"prefix": "raw/screen_time/v2/"},
            {"prefix": "raw/screen_time/v1/_control/collector/latest/"},
        ]
    finally:
        warehouse.close()

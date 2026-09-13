from __future__ import annotations

import gzip
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.loader.job import run_loader
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.screen_time.raw import (
    ScreenTimeRawIdentity,
    parse_raw_object_key,
)
from personal_data_platform.storage.gcs import GCSRawRepository
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


class _Repository:
    def __init__(self, observations: list[RawObject], objects: dict[str, bytes]) -> None:
        self.observations = observations
        self.objects = objects
        self.get_calls: list[tuple[str, int]] = []

    def list_raw(self, prefix: str) -> list[RawObject]:
        return [raw for raw in reversed(self.observations) if raw.key.startswith(prefix)]

    def get_raw(self, key: str, *, generation: int) -> bytes:
        self.get_calls.append((key, generation))
        return self.objects[key]


def _raw(
    key: str,
    content: bytes,
    observed_at: datetime,
    *,
    storage_generation: int = 1,
) -> RawObject:
    identity = ScreenTimeRawIdentity(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key=hashlib.sha256(key.encode()).hexdigest(),
        observed_at=observed_at,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    return parse_raw_object_key(
        identity.object_key,
        storage_created_at=observed_at,
        storage_generation=storage_generation,
    )


def test_loader_continues_after_poison_object_and_retries_it(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    good_content = b"good"
    repaired_content = b"expected"
    good = _raw("raw/good.segb.gz", good_content, now)
    poison = _raw("raw/poison.segb.gz", repaired_content, now + timedelta(seconds=1))
    repository = _Repository(
        [good, poison],
        {
            good.key: gzip.compress(good_content),
            poison.key: gzip.compress(b"wrong"),
        },
    )
    monkeypatch.setattr(
        "personal_data_platform.sources.screen_time.adapter.parse_segb_bytes",
        lambda raw, segment, **kwargs: [],
    )
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "loader.duckdb"))))
    warehouse.migrate()
    try:
        first = run_loader(repository, warehouse)

        assert first.succeeded == 1
        assert first.failed == 1
        assert not first.ok
        assert warehouse.ingestion_counts(source_id="screen_time", stream="app-in-focus") == {
            "failed": 1,
            "succeeded": 1,
        }

        repository.objects[poison.key] = gzip.compress(repaired_content)
        second = run_loader(repository, warehouse)

        assert second.skipped == 1
        assert second.succeeded == 1
        assert second.failed == 0
        assert second.ok
        assert warehouse.ingestion_counts(source_id="screen_time", stream="app-in-focus") == {
            "succeeded": 2
        }
    finally:
        warehouse.close()


def test_loader_revalidates_a_recreated_object_generation(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    content = b"same-content"
    original = _raw("raw/recreated.segb.gz", content, now, storage_generation=1)
    repository = _Repository([original], {original.key: gzip.compress(content)})
    monkeypatch.setattr(
        "personal_data_platform.sources.screen_time.adapter.parse_segb_bytes",
        lambda raw, segment, **kwargs: [],
    )
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "loader.duckdb"))))
    warehouse.migrate()
    try:
        assert run_loader(repository, warehouse).succeeded == 1

        recreated = replace(
            original,
            storage_created_at=now + timedelta(days=91),
            storage_generation=2,
        )
        repository.observations = [recreated]

        second = run_loader(repository, warehouse)

        assert second.skipped == 0
        assert second.succeeded == 1
        assert repository.get_calls[-1] == (original.key, 2)
        assert (
            warehouse.query_value(
                "SELECT storage_generation FROM ops.ingestion_metadata WHERE object_key = ?",
                [original.key],
            )
            == 2
        )
    finally:
        warehouse.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "another_source"},
        {"stream": "another-stream"},
        {"schema_version": 2},
        {"subject_key": "b" * 64},
    ],
)
def test_loader_rejects_mixed_or_inconsistent_metadata_before_reading(tmp_path, changes) -> None:
    content = b"unread"
    raw = _raw("identity", content, datetime(2026, 8, 27, tzinfo=UTC))
    repository = _Repository([replace(raw, **changes)], {raw.key: gzip.compress(content)})
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "rejected.duckdb"))))
    warehouse.migrate()
    try:
        with pytest.raises(ValueError, match="selected source/stream|metadata does not match"):
            run_loader(repository, warehouse)
        assert repository.get_calls == []
        assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 0
    finally:
        warehouse.close()


class _JsonBatch:
    parser_version = "synthetic-json-v1"
    record_count = 1

    def __init__(self, value: int) -> None:
        self.value = value

    def write(self, connection, raw, *, byte_size, loaded_at) -> None:
        connection.execute(
            "INSERT INTO base.synthetic_value VALUES (?, ?, ?, ?)",
            [raw.key, raw.stream, raw.schema_version, self.value],
        )


class _JsonSource:
    source_id = "synthetic"
    schema_versions = (1, 2)
    raw_prefixes = ("raw/synthetic/v1/", "raw/synthetic/v2/")
    raw_suffixes = (".json.gz",)

    def __init__(self, stream: str = "activity") -> None:
        self.stream = stream
        self.decoded_versions: list[int] = []

    def validate_raw_key(self, key: str) -> None:
        parts = key.split("/")
        if (
            len(parts) != 8
            or parts[:2] != ["raw", "synthetic"]
            or parts[2] not in {"v1", "v2"}
            or parts[4] not in {"activity", "sleep"}
            or not key.endswith(".json.gz")
        ):
            raise ValueError("unsupported synthetic Raw key")

    def parse_raw_key(self, key: str, *, storage_created_at, storage_generation) -> RawObject:
        self.validate_raw_key(key)
        parts = key.split("/")
        return RawObject(
            key=key,
            source_id=self.source_id,
            schema_version=int(parts[2][1:]),
            subject_key=parts[3],
            stream=parts[4],
            logical_key=parts[5],
            observed_at=datetime.fromisoformat(parts[6]),
            sha256=parts[7].removesuffix(".json.gz"),
            storage_created_at=storage_created_at,
            storage_generation=storage_generation,
        )

    def decode(self, raw: RawObject, payload: bytes) -> _JsonBatch:
        import json

        self.decoded_versions.append(raw.schema_version)
        decoded = json.loads(payload)
        field = "value" if raw.schema_version == 1 else "measurement"
        return _JsonBatch(int(decoded[field]))

    def legacy_scope(self, raw: RawObject) -> None:
        return None


def _json_raw(source, payload: bytes, version: int, now: datetime) -> RawObject:
    digest = hashlib.sha256(payload).hexdigest()
    key = (
        f"raw/synthetic/v{version}/account/{source.stream}/daily/{now.isoformat()}/{digest}.json.gz"
    )
    return source.parse_raw_key(key, storage_created_at=now, storage_generation=1)


class _SyntheticGCSClient:
    def __init__(self, observations: list[RawObject], objects: dict[str, bytes]) -> None:
        self.observations = observations
        self.objects = objects
        self.list_calls: list[str] = []

    def bucket(self, name):
        assert name == "synthetic-bucket"
        return self

    def list_blobs(self, bucket, *, prefix):
        assert bucket is self
        self.list_calls.append(prefix)
        return SimpleNamespace(
            pages=[
                [
                    SimpleNamespace(
                        name=raw.key,
                        time_created=raw.storage_created_at,
                        generation=raw.storage_generation,
                    )
                ]
                for raw in self.observations
                if raw.key.startswith(prefix)
            ]
        )

    def blob(self, key, *, generation):
        def download_as_bytes(*, raw_download, if_generation_match):
            assert raw_download is True
            assert generation == if_generation_match == 1
            return self.objects[key]

        return SimpleNamespace(download_as_bytes=download_as_bytes)


def test_loader_uses_source_decoder_across_schema_versions_and_preserves_other_stream(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    source = _JsonSource()
    v1 = b'{"value": 12}'
    v2 = b'{"measurement": 34}'
    first = _json_raw(source, v1, 1, now)
    second = _json_raw(source, v2, 2, now + timedelta(seconds=1))
    other_source = _JsonSource(stream="sleep")
    poison = _json_raw(other_source, b"invalid-json", 1, now)
    client = _SyntheticGCSClient(
        [second, poison, first],
        {
            first.key: gzip.compress(v1),
            second.key: gzip.compress(v2),
            poison.key: gzip.compress(b"invalid-json"),
        },
    )
    repository = GCSRawRepository(client=client, bucket="synthetic-bucket", source=source)
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "synthetic.duckdb"))))
    warehouse.migrate()
    warehouse.connection.execute(
        "CREATE TABLE base.synthetic_value (object_key VARCHAR, stream VARCHAR, "
        "schema_version INTEGER, value INTEGER)"
    )
    try:
        result = run_loader(repository, warehouse, source=source)
        assert (result.succeeded, result.records) == (2, 2)
        assert source.decoded_versions == [1, 2]
        assert warehouse.query_rows(
            "SELECT schema_version, value FROM base.synthetic_value ORDER BY schema_version"
        ) == [(1, 12), (2, 34)]
        assert run_loader(repository, warehouse, source=source).skipped == 2

        assert set(client.list_calls) == set(source.raw_prefixes)
        other_repository = GCSRawRepository(
            client=client, bucket="synthetic-bucket", source=other_source
        )
        assert run_loader(other_repository, warehouse, source=other_source).failed == 1
        assert warehouse.ingestion_counts(source_id="synthetic", stream="activity") == {
            "succeeded": 2
        }
        assert warehouse.ingestion_counts(source_id="synthetic", stream="sleep") == {"failed": 1}
        assert run_loader(repository, warehouse, source=source).skipped == 2
        assert warehouse.query_value("SELECT count(*) FROM base.synthetic_value") == 2
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM ops.job_run WHERE job_name = 'loader:synthetic:activity'"
            )
            == 3
        )

        phone_content = b"synthetic-segb"
        phone = _raw("phone", phone_content, now)
        phone_repository = _Repository([phone], {phone.key: gzip.compress(phone_content)})
        monkeypatch.setattr(
            "personal_data_platform.sources.screen_time.adapter.parse_segb_bytes",
            lambda raw, payload, **kwargs: [],
        )
        assert run_loader(phone_repository, warehouse).succeeded == 1
        assert warehouse.ingestion_counts(source_id="screen_time", stream="app-in-focus") == {
            "succeeded": 1
        }
        assert warehouse.ingestion_counts(source_id="synthetic", stream="activity") == {
            "succeeded": 2
        }
    finally:
        warehouse.close()


@pytest.mark.parametrize(
    ("name", "value"),
    [("PDP_RAW_RETENTION_DAYS", "91"), ("PDP_LIFECYCLE_GRACE_DAYS", "4")],
)
def test_loader_runtime_rejects_retention_drift_before_cloud_access(
    monkeypatch, name, value
) -> None:
    from personal_data_platform.loader.job import run_loader_from_env
    from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource

    monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        ScreenTimeSource,
        "repository_from_env",
        lambda self: pytest.fail("cloud repository must not be constructed"),
    )
    with pytest.raises(ValueError, match=name):
        run_loader_from_env()

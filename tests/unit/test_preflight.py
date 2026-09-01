from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from personal_data_platform.preflight import probe_gcs, probe_warehouse, run_preflight_from_env


class _Blob:
    def __init__(self, bucket: _Bucket, name: str, generation: int | None = None) -> None:
        self._bucket = bucket
        self.name = name
        self.generation = generation

    def upload_from_string(self, data: bytes, **kwargs: Any) -> None:
        self._bucket.upload_calls.append({"name": self.name, **kwargs})
        self._bucket.objects[self.name] = data
        self.generation = self._bucket.upload_generation

    def download_as_bytes(self, **kwargs: Any) -> bytes:
        self._bucket.download_calls.append(
            {"name": self.name, "generation": self.generation, **kwargs}
        )
        if self._bucket.download_error is not None:
            raise self._bucket.download_error
        return self._bucket.objects[self.name]

    def delete(self, **kwargs: Any) -> None:
        self._bucket.delete_calls.append(
            {"name": self.name, "generation": self.generation, **kwargs}
        )
        if self._bucket.delete_error is not None:
            raise self._bucket.delete_error
        del self._bucket.objects[self.name]


class _Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.upload_generation: int | None = 7
        self.upload_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.download_error: Exception | None = None
        self.delete_error: Exception | None = None

    def blob(self, name: str, generation: int | None = None) -> _Blob:
        return _Blob(self, name, generation)


class _GCSClient:
    def __init__(self) -> None:
        self.bucket_ref = _Bucket()

    def bucket(self, name: str) -> _Bucket:
        assert name == "preflight-bucket"
        return self.bucket_ref

    def list_blobs(self, bucket: _Bucket, *, prefix: str):
        assert bucket is self.bucket_ref
        return [SimpleNamespace(name=key) for key in bucket.objects if key.startswith(prefix)]


def test_gcs_probe_cleans_up_exact_uploaded_generation() -> None:
    client = _GCSClient()

    result = probe_gcs(client, bucket="preflight-bucket")

    assert result["ok"] is True
    assert client.bucket_ref.objects == {}
    assert client.bucket_ref.upload_calls[0]["if_generation_match"] == 0
    assert client.bucket_ref.download_calls[0]["generation"] == 7
    assert client.bucket_ref.download_calls[0]["if_generation_match"] == 7
    assert client.bucket_ref.delete_calls[0]["generation"] == 7
    assert client.bucket_ref.delete_calls[0]["if_generation_match"] == 7


def test_gcs_cleanup_failure_fails_the_probe() -> None:
    client = _GCSClient()
    client.bucket_ref.delete_error = RuntimeError("cleanup denied")

    with pytest.raises(RuntimeError, match="cleanup denied"):
        probe_gcs(client, bucket="preflight-bucket")


def test_gcs_cleanup_failure_preserves_original_probe_error(caplog) -> None:
    client = _GCSClient()
    client.bucket_ref.download_error = RuntimeError("download failed")
    client.bucket_ref.delete_error = RuntimeError("cleanup denied")

    with pytest.raises(RuntimeError, match="download failed"):
        probe_gcs(client, bucket="preflight-bucket")
    assert "cleanup denied" in caplog.text


def test_gcs_missing_upload_generation_never_uses_unqualified_delete() -> None:
    client = _GCSClient()
    client.bucket_ref.upload_generation = None

    with pytest.raises(RuntimeError, match="generation"):
        probe_gcs(client, bucket="preflight-bucket")

    assert client.bucket_ref.delete_calls == []


def test_preflight_rejects_production_bucket_before_connecting(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "synthetic-project")
    monkeypatch.setenv("GCS_BUCKET", "same-bucket")
    monkeypatch.setenv("GCS_PREFLIGHT_BUCKET", "same-bucket")

    with pytest.raises(ValueError, match="must differ"):
        run_preflight_from_env()


def test_warehouse_probe_cleans_up_its_table() -> None:
    import duckdb

    connection = duckdb.connect(":memory:")
    result = probe_warehouse(connection)

    assert result["ok"] is True
    assert (
        connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'preflight'"
        ).fetchone()[0]
        == 0
    )

from __future__ import annotations

import io

import pytest

from personal_data_platform.preflight import probe_b2, probe_warehouse


class _B2Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted_versions: list[str] = []

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ServerSideEncryption: str,
    ) -> dict[str, str]:
        self.objects[Key] = Body
        return {"VersionId": "probe-upload-version"}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, io.BytesIO]:
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, *, Bucket: str, Prefix: str) -> dict[str, object]:
        return {"Contents": [{"Key": key} for key in self.objects if key.startswith(Prefix)]}

    def delete_object(self, *, Bucket: str, Key: str, VersionId: str | None = None) -> None:
        # B2 retains the uploaded version if this only creates a delete marker.
        if VersionId is not None:
            self.deleted_versions.append(VersionId)
            del self.objects[Key]


def test_b2_probe_cleans_up_its_test_object() -> None:
    client = _B2Client()

    result = probe_b2(client, bucket="test")

    assert result["ok"] is True
    assert client.objects == {}
    assert client.deleted_versions == ["probe-upload-version"]


def test_b2_cleanup_failure_fails_the_probe(monkeypatch) -> None:
    client = _B2Client()

    def fail(**_):
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(client, "delete_object", fail)
    with pytest.raises(RuntimeError, match="cleanup denied"):
        probe_b2(client, bucket="test")


def test_b2_cleanup_failure_preserves_original_probe_error(monkeypatch, caplog) -> None:
    client = _B2Client()

    def fail_read(**_):
        raise RuntimeError("download failed")

    def fail_delete(**_):
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(client, "get_object", fail_read)
    monkeypatch.setattr(client, "delete_object", fail_delete)
    with pytest.raises(RuntimeError, match="download failed"):
        probe_b2(client, bucket="test")
    assert "cleanup denied" in caplog.text


def test_b2_missing_upload_version_never_uses_unversioned_delete(monkeypatch) -> None:
    client = _B2Client()
    monkeypatch.setattr(client, "put_object", lambda **_: {})

    with pytest.raises(RuntimeError, match="VersionId"):
        probe_b2(client, bucket="test")

    assert client.deleted_versions == []


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

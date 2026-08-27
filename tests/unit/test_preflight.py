from __future__ import annotations

import io

from personal_data_platform.preflight import probe_b2, probe_warehouse


class _B2Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ServerSideEncryption: str,
    ) -> None:
        self.objects[Key] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, io.BytesIO]:
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, *, Bucket: str, Prefix: str) -> dict[str, object]:
        return {"Contents": [{"Key": key} for key in self.objects if key.startswith(Prefix)]}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        del self.objects[Key]


def test_b2_probe_cleans_up_its_test_object() -> None:
    client = _B2Client()

    result = probe_b2(client, bucket="test")

    assert result["ok"] is True
    assert client.objects == {}


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

import gzip
from datetime import UTC, datetime
from io import BytesIO

from personal_data_platform.raw.screen_time import ScreenTimeRawIdentity, sha256_hex
from personal_data_platform.storage.b2 import B2RawRepository, CollectorScanReceipt


class FakeB2Client:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, object]] = []
        self.get_body = b""
        self.get_bodies: dict[str, bytes] = {}
        self.list_responses: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> None:
        self.put_calls.append(kwargs)

    def get_object(self, **kwargs: object) -> dict[str, BytesIO]:
        key = str(kwargs["Key"])
        return {"Body": BytesIO(self.get_bodies.get(key, self.get_body))}

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        self.list_calls.append(kwargs)
        return self.list_responses.pop(0)


def _identity(observed_at: datetime, marker: str = "a") -> ScreenTimeRawIdentity:
    return ScreenTimeRawIdentity(
        device_key="1" * 64,
        stream="app-in-focus",
        segment_key="2" * 64,
        observed_at=observed_at,
        sha256=marker * 64,
    )


def test_store_raw_uses_only_put_and_enables_sse_b2() -> None:
    client = FakeB2Client()
    repository = B2RawRepository(client=client, bucket="synthetic-bucket")
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
    assert len(client.put_calls) == 1
    assert gzip.decompress(client.put_calls[0]["Body"]) == raw_bytes
    assert client.put_calls[0]["ServerSideEncryption"] == "AES256"
    assert client.list_calls == []


def test_get_raw_returns_stored_gzip_bytes_for_loader_verification() -> None:
    client = FakeB2Client()
    client.get_body = gzip.compress(b"synthetic-segb", mtime=0)
    repository = B2RawRepository(client=client, bucket="synthetic-bucket")

    downloaded = repository.get_raw(_identity(datetime(2026, 8, 27, tzinfo=UTC)).object_key)

    assert downloaded == client.get_body


def test_list_raw_follows_pages_ignores_other_objects_and_sorts_replay_order() -> None:
    client = FakeB2Client()
    earlier = _identity(datetime(2026, 8, 27, 1, tzinfo=UTC), "a")
    later = _identity(datetime(2026, 8, 27, 2, tzinfo=UTC), "b")
    client.list_responses = [
        {
            "Contents": [{"Key": later.object_key}, {"Key": "diagnostics/not-raw"}],
            "IsTruncated": True,
            "NextContinuationToken": "page-2",
        },
        {"Contents": [{"Key": earlier.object_key}], "IsTruncated": False},
    ]
    repository = B2RawRepository(client=client, bucket="synthetic-bucket")

    observations = repository.list_raw()

    assert [item.key for item in observations] == [earlier.object_key, later.object_key]
    assert client.list_calls[1]["ContinuationToken"] == "page-2"


def test_scan_receipt_round_trip_is_pseudonymized_and_listed() -> None:
    client = FakeB2Client()
    repository = B2RawRepository(client=client, bucket="synthetic-bucket")
    receipt = CollectorScanReceipt(
        device_key="1" * 64,
        completed_at=datetime(2026, 8, 27, tzinfo=UTC),
        segment_count=3,
    )

    repository.put_scan_receipt(receipt)

    call = client.put_calls[0]
    assert call["Key"] == receipt.key
    assert call["ServerSideEncryption"] == "AES256"
    assert b"synthetic" not in call["Body"]

    client.get_bodies[receipt.key] = call["Body"]
    client.list_responses = [{"Contents": [{"Key": receipt.key}], "IsTruncated": False}]
    assert repository.list_scan_receipts() == [receipt]

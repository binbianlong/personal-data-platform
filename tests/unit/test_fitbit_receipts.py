"""Durability and generation guards for lightweight notification receipts."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from google.api_core.exceptions import PreconditionFailed

from personal_data_platform.sources.fitbit.models import Window
from personal_data_platform.sources.fitbit.receipts import (
    GCSReceiptRepository,
    Receipt,
    ReceiptWork,
    validate_receipt_key,
)

NOW = datetime(2026, 9, 27, tzinfo=UTC)
WINDOW = Window("steps", NOW, NOW.replace(hour=1))


class Blob:
    content_encoding = None

    def __init__(self, bucket, name, generation=None):
        self.bucket = bucket
        self.name = name
        self.generation = generation
        self.time_created = NOW
        self.metadata = None

    @property
    def metadata(self):
        return None if self._metadata is None else dict(self._metadata)

    @metadata.setter
    def metadata(self, value):
        self._metadata = None if value is None else dict(value)

    def upload_from_string(self, data, *, content_type, if_generation_match):
        stored = self.bucket.values.get(self.name)
        actual = stored[0] if stored else 0
        if actual != if_generation_match:
            raise PreconditionFailed("generation conflict")
        self.generation = actual + 1
        self.bucket.values[self.name] = (self.generation, data, dict(self.metadata))

    def download_as_bytes(self, *, raw_download, if_generation_match):
        generation, data, _ = self.bucket.values[self.name]
        assert raw_download
        if generation != if_generation_match:
            raise PreconditionFailed("generation conflict")
        self.bucket.downloads += 1
        return data


class Client:
    def __init__(self):
        self.values = {}
        self.downloads = 0

    def bucket(self, name):
        return self

    def blob(self, name, *, generation=None):
        return Blob(self, name, generation)

    def get_blob(self, name):
        if name not in self.values:
            return None
        generation, _, metadata = self.values[name]
        blob = self.blob(name, generation=generation)
        blob.metadata = metadata
        return blob

    def list_blobs(self, bucket, *, prefix):
        return [self.get_blob(name) for name in sorted(self.values) if name.startswith(prefix)]


def receipt(*, daily=False):
    return Receipt.create("subject", (WINDOW,), received_at=NOW, daily=daily)


def test_every_delivery_has_independent_receipt_and_safe_payload() -> None:
    first, second = receipt(), receipt()
    assert first.key != second.key
    assert first.work == (ReceiptWork(WINDOW),)
    encoded = first.to_bytes()
    assert Receipt.from_bytes(encoded) == first
    assert "healthUserId" not in json.loads(encoded)


def test_cas_retains_progress_and_daily_receipt_is_create_once() -> None:
    client = Client()
    repository = GCSReceiptRepository(client=client, bucket="bucket")
    original = repository.create(receipt(daily=True))
    assert repository.create(receipt(daily=True)) == original
    completed = replace(
        original.receipt, work=(ReceiptWork(WINDOW, completed=True),), completed_at=NOW
    )
    updated = repository.replace(original, completed)
    assert updated.generation > original.generation
    assert repository.read(original.receipt.key) == updated
    with pytest.raises(PreconditionFailed):
        repository.replace(original, completed)


def test_inventory_downloads_only_pending_and_reports_staleness() -> None:
    client = Client()
    repository = GCSReceiptRepository(client=client, bucket="bucket")
    pending = repository.create(receipt())
    completed = repository.create(receipt())
    repository.replace(
        completed,
        replace(completed.receipt, work=(ReceiptWork(WINDOW, completed=True),), completed_at=NOW),
    )
    before = client.downloads
    inventory = repository.inventory()
    assert inventory.pending == (pending,)
    assert inventory.latest_received_at == NOW
    assert inventory.latest_completed_at == NOW
    assert client.downloads - before == 1


@pytest.mark.parametrize(
    "key", ["../secret", "raw/fitbit/object.json", "receipts/fitbit/v1/2026-09-27/bad.json"]
)
def test_receipt_keys_cannot_read_arbitrary_bucket_objects(key: str) -> None:
    with pytest.raises(ValueError):
        validate_receipt_key(key)

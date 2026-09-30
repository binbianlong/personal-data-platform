"""Durability and generation guards for lightweight notification receipts."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed, ServiceUnavailable

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


def test_physical_work_uses_tokyo_day_boundary():
    start = datetime(2026, 9, 28, tzinfo=ZoneInfo("Asia/Tokyo"))
    end = start + timedelta(days=1)
    receipt = Receipt.create("subject", (Window("steps", start, end),), received_at=NOW)
    assert len(receipt.work) == 1
    assert receipt.work[0].window == Window("steps", start, end)


def test_skipped_work_retains_observation_evidence_and_reads_legacy_v1() -> None:
    original = receipt()
    old_payload = original.to_bytes()
    assert Receipt.from_bytes(old_payload) == original
    observed = ReceiptWork(WINDOW, completed=True, fetched_at=NOW, source_sha256="a" * 64)
    updated = replace(original, work=(observed,), completed_at=NOW)
    assert Receipt.from_bytes(updated.to_bytes()) == updated
    assert Receipt.from_bytes(old_payload).work[0].source_sha256 is None


@pytest.mark.parametrize(
    ("identity", "origin"),
    [
        ("bootstrap", "bootstrap"),
        ("weekly", "weekly"),
        ("device-0123456789ab", "device-sync"),
    ],
)
def test_deterministic_repair_receipts_have_safe_keys(identity: str, origin: str) -> None:
    key = f"receipts/fitbit/v1/2026-09-27/{identity}.json"
    value = Receipt.create("subject", (WINDOW,), received_at=NOW, key=key, origin=origin)
    assert value.key == key
    assert Receipt.from_bytes(value.to_bytes()) == value


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


def _race_download(monkeypatch, key, change, *, error=PreconditionFailed, conflicts=1):
    original = Blob.download_as_bytes
    generations = []

    def download(blob, **kwargs):
        if blob.name == key:
            generations.append(kwargs["if_generation_match"])
            if len(generations) <= conflicts:
                change()
                raise error("listed generation no longer exists")
        return original(blob, **kwargs)

    monkeypatch.setattr(Blob, "download_as_bytes", download)
    return generations


@pytest.mark.parametrize("error", [NotFound, PreconditionFailed])
@pytest.mark.parametrize("operation", ["read", "inventory"])
def test_receipt_read_refreshes_changed_generation(monkeypatch, error, operation):
    client = Client()
    repository = GCSReceiptRepository(client=client, bucket="test")
    stored = repository.create(receipt())
    updated = []

    def change():
        updated.append(
            repository.replace(
                stored, replace(stored.receipt, received_at=NOW + timedelta(seconds=1))
            )
        )

    generations = _race_download(monkeypatch, stored.receipt.key, change, error=error)
    if operation == "read":
        result = repository.read(stored.receipt.key)
    else:
        result = repository.inventory().pending[0]
    assert result == updated[0]
    assert generations == [1, 2]


def test_inventory_refreshes_completed_metadata_without_downloading_body(monkeypatch):
    client = Client()
    repository = GCSReceiptRepository(client=client, bucket="test")
    stored = repository.create(receipt())
    other = repository.create(receipt())
    completed_at = NOW + timedelta(seconds=10)

    def complete():
        repository.replace(
            stored,
            replace(
                stored.receipt,
                work=(ReceiptWork(WINDOW, completed=True),),
                completed_at=completed_at,
            ),
        )

    generations = _race_download(monkeypatch, stored.receipt.key, complete)
    inventory = repository.inventory()
    assert inventory.pending == (other,)
    assert inventory.latest_completed_at == completed_at
    assert inventory.deferred_count == 0
    assert generations == [1]
    assert client.downloads == 1


@pytest.mark.parametrize("operation", ["read", "inventory"])
def test_receipt_disappearance_requires_fresh_metadata(monkeypatch, operation):
    client = Client()
    repository = GCSReceiptRepository(client=client, bucket="test")
    stored = repository.create(receipt())
    other = repository.create(receipt())
    generations = _race_download(
        monkeypatch,
        stored.receipt.key,
        lambda: client.values.pop(stored.receipt.key),
        error=NotFound,
    )
    if operation == "read":
        with pytest.raises(FileNotFoundError):
            repository.read(stored.receipt.key)
    else:
        inventory = repository.inventory()
        assert inventory.pending == (other,)
        assert inventory.deferred_count == 0
    assert generations == [1]


@pytest.mark.parametrize("conflicts", [2, 3])
@pytest.mark.parametrize("operation", ["read", "inventory"])
def test_receipt_read_is_bounded_and_defers_only_changing_entry(
    monkeypatch, caplog, conflicts, operation
):
    from personal_data_platform.sources.fitbit import receipts as module

    client = Client()
    repository = GCSReceiptRepository(client=client, bucket="test")
    current = [repository.create(receipt())]
    other = repository.create(receipt())

    def change():
        stored = current[0]
        current[0] = repository.replace(
            stored,
            replace(stored.receipt, received_at=stored.receipt.received_at + timedelta(seconds=1)),
        )

    generations = _race_download(
        monkeypatch, current[0].receipt.key, change, error=NotFound, conflicts=conflicts
    )
    if operation == "read" and conflicts == 3:
        with pytest.raises(module.ReceiptReadConflict):
            repository.read(current[0].receipt.key)
    elif operation == "read":
        assert repository.read(current[0].receipt.key) == current[0]
    else:
        inventory = repository.inventory()
        if conflicts == 3:
            assert inventory.pending == (other,)
            assert inventory.deferred_count == 1
            assert any(record.levelname == "WARNING" for record in caplog.records)
        else:
            assert set(inventory.pending) == {other, current[0]}
            assert inventory.deferred_count == 0
    assert generations == [1, 2, 3]


@pytest.mark.parametrize("error", [Forbidden, ServiceUnavailable, OSError])
@pytest.mark.parametrize("operation", ["read", "inventory"])
def test_receipt_read_does_not_hide_non_race_errors(monkeypatch, error, operation):
    client = Client()
    repository = GCSReceiptRepository(client=client, bucket="test")
    stored = repository.create(receipt())
    generations = _race_download(monkeypatch, stored.receipt.key, lambda: None, error=error)
    with pytest.raises(error):
        if operation == "read":
            repository.read(stored.receipt.key)
        else:
            repository.inventory()
    assert generations == [1]


@pytest.mark.parametrize("bad_body", [b"{", b"{}"])
def test_inventory_does_not_hide_invalid_receipt_body(bad_body):
    client = Client()
    repository = GCSReceiptRepository(client=client, bucket="test")
    stored = repository.create(receipt())
    generation, _, metadata = client.values[stored.receipt.key]
    client.values[stored.receipt.key] = (generation, bad_body, metadata)
    with pytest.raises((ValueError, KeyError)):
        repository.inventory()


def test_receipt_read_does_not_hide_body_key_mismatch():
    client = Client()
    repository = GCSReceiptRepository(client=client, bucket="test")
    stored = repository.create(receipt())
    generation, _, metadata = client.values[stored.receipt.key]
    client.values[stored.receipt.key] = (generation, receipt().to_bytes(), metadata)
    with pytest.raises(ValueError, match="object key"):
        repository.inventory()

from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.raw import ScreenTimeRawIdentity
from personal_data_platform.storage.gcs import GCSRawRepository


class Blob:
    def __init__(self, bucket, name, generation=None):
        self.bucket, self.name, self.generation = bucket, name, generation
        self.time_created = None
        self.custom_time = None
        self.content_encoding = None

    def reload(self):
        if self.name not in self.bucket.objects:
            raise NotFound("missing generation")
        data, generation, created, origin = self.bucket.objects[self.name]
        if self.generation is not None and self.generation != generation:
            raise NotFound("missing generation")
        self.generation, self.time_created, self.custom_time = generation, created, origin

    def download_as_bytes(self, *, raw_download, if_generation_match=None):
        assert raw_download
        self.reload()
        if if_generation_match is not None and if_generation_match != self.generation:
            raise NotFound("missing generation")
        return self.bucket.objects[self.name][0]

    def upload_from_string(self, data, *, if_generation_match, **kwargs):
        assert if_generation_match == 0
        if self.name in self.bucket.objects:
            raise PreconditionFailed("already exists")
        self.bucket.writes += 1
        if self.bucket.fail_after is not None and self.bucket.writes > self.bucket.fail_after:
            raise RuntimeError("interrupted")
        generation = 100 + self.bucket.writes
        self.bucket.objects[self.name] = (data, generation, self.bucket.created, self.custom_time)
        self.reload()


class Bucket:
    def __init__(self):
        self.objects = {}
        self.created = datetime(2026, 10, 5, tzinfo=UTC)
        self.writes = 0
        self.fail_after = None

    def blob(self, name, *, generation=None):
        return Blob(self, name, generation)

    def get_blob(self, name):
        if name not in self.objects:
            return None
        blob = self.blob(name)
        blob.reload()
        return blob


class Client:
    def __init__(self):
        self.buckets = {"old": Bucket(), "west": Bucket()}

    def bucket(self, name):
        return self.buckets[name]

    def list_blobs(self, bucket, *, prefix):
        blobs = [bucket.get_blob(key) for key in sorted(bucket.objects) if key.startswith(prefix)]
        return type("Listing", (), {"pages": [blobs]})()


def fixture_objects(client, count=1):
    origin = datetime(2026, 7, 7, tzinfo=UTC)
    for index in range(count):
        data = f"synthetic segment {index}".encode()
        key = ScreenTimeRawIdentity(
            device_key="a" * 64,
            stream="app-in-focus",
            segment_key=f"{index:064x}",
            observed_at=origin + timedelta(minutes=index),
            sha256=hashlib.sha256(data).hexdigest(),
        ).object_key
        client.buckets["old"].objects[key] = (gzip.compress(data, mtime=0), index + 1, origin, None)
    return origin


def test_copy_preserves_retention_origin_and_changes_generation(tmp_path):
    from scripts.migrate_west_raw import copy_manifest, inventory, verify_manifest

    client = Client()
    original_created_at = fixture_objects(client)
    path = tmp_path / "manifest.json"
    inventory(client, source_bucket="old", target_bucket="west", manifest_path=path)
    copy_manifest(client, path)
    source = get_source("screen_time", "app-in-focus")
    raw = GCSRawRepository(client=client, bucket="west", source=source).list_raw()[0]
    assert raw.retention_started_at == original_created_at
    assert raw.storage_created_at == datetime(2026, 10, 5, tzinfo=UTC)
    assert raw.storage_generation == 101
    assert verify_manifest(client, path) == 1
    copy_manifest(client, path)
    assert client.buckets["west"].writes == 1


def test_copy_resumes_after_interruption_without_rewriting(tmp_path):
    from scripts.migrate_west_raw import copy_manifest, inventory, verify_manifest

    client = Client()
    fixture_objects(client, 2)
    path = tmp_path / "manifest.json"
    inventory(client, source_bucket="old", target_bucket="west", manifest_path=path)
    client.buckets["west"].fail_after = 1
    with pytest.raises(RuntimeError, match="interrupted"):
        copy_manifest(client, path)
    client.buckets["west"].fail_after = None
    copy_manifest(client, path)
    assert verify_manifest(client, path) == 2
    assert len(client.buckets["west"].objects) == 2


@pytest.mark.parametrize("corruption", ["generation", "hash", "target_hash", "target_origin"])
def test_copy_or_verify_rejects_changed_generation_hash_or_origin(tmp_path, corruption):
    from scripts.migrate_west_raw import copy_manifest, inventory, verify_manifest

    client = Client()
    fixture_objects(client)
    path = tmp_path / "manifest.json"
    inventory(client, source_bucket="old", target_bucket="west", manifest_path=path)
    key = next(iter(client.buckets["old"].objects))
    data, generation, created, origin = client.buckets["old"].objects[key]
    if corruption == "generation":
        client.buckets["old"].objects[key] = (data, generation + 1, created, origin)
    elif corruption == "hash":
        client.buckets["old"].objects[key] = (
            gzip.compress(b"corrupt"),
            generation,
            created,
            origin,
        )
    else:
        copy_manifest(client, path)
        data, generation, created, origin = client.buckets["west"].objects[key]
        if corruption == "target_hash":
            data = gzip.compress(b"corrupt")
        else:
            origin = created
        client.buckets["west"].objects[key] = (data, generation, created, origin)
    with pytest.raises((RuntimeError, ValueError, NotFound)):
        if corruption.startswith("target"):
            verify_manifest(client, path)
        else:
            copy_manifest(client, path)


def test_manifest_rejects_fitbit_and_unknown_fields_before_copy(tmp_path):
    import json

    from scripts.migrate_west_raw import copy_manifest, inventory

    client = Client()
    fixture_objects(client)
    path = tmp_path / "manifest.json"
    inventory(client, source_bucket="old", target_bucket="west", manifest_path=path)
    document = json.loads(path.read_text())
    document["objects"][0]["key"] = "raw/fitbit/v1/unsafe.json.gz"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        copy_manifest(client, path)
    assert not client.buckets["west"].objects


@pytest.mark.parametrize("age,lag,overdue", [(90, 1, 0), (93, 0, 1)])
def test_copied_raw_audit_uses_original_90_and_93_day_deadlines(age, lag, overdue):
    from dataclasses import replace

    from personal_data_platform.reconciliation.job import run_reconciliation
    from tests.unit.test_reconciliation import NOW, _key, _Repository, _Warehouse

    key = _key("copied")
    repository = _Repository([key])
    original_list = repository.list_raw
    repository.list_raw = lambda prefix: [
        replace(raw, storage_created_at=NOW, retention_started_at=NOW - timedelta(days=age))
        for raw in original_list(prefix)
    ]
    warehouse = _Warehouse({key})
    result = run_reconciliation(repository, warehouse, heartbeat=lambda details: None, now=NOW)
    assert result.details["lifecycle_lag_object_count"] == lag
    assert result.details["overdue_deletion_object_count"] == overdue


def test_copy_recovers_success_when_manifest_save_was_interrupted(tmp_path, monkeypatch):
    from scripts import migrate_west_raw as migration

    client = Client()
    fixture_objects(client)
    path = tmp_path / "manifest.json"
    migration.inventory(client, source_bucket="old", target_bucket="west", manifest_path=path)
    save = migration.save_manifest

    def interrupted(*args):
        raise RuntimeError("cursor save interrupted")

    monkeypatch.setattr(migration, "save_manifest", interrupted)
    with pytest.raises(RuntimeError, match="cursor save interrupted"):
        migration.copy_manifest(client, path)
    monkeypatch.setattr(migration, "save_manifest", save)
    migration.copy_manifest(client, path)
    assert migration.verify_manifest(client, path) == 1
    assert client.buckets["west"].writes == 1


def test_inventory_rejects_artifact_path_in_tracked_repository_before_access():
    from pathlib import Path

    from scripts.migrate_west_raw import inventory

    class NoAccess:
        def bucket(self, name):
            pytest.fail("invalid artifact destination must fail before cloud access")

    path = Path(__file__).resolve().parents[2] / "migration-manifest.json"
    with pytest.raises(ValueError, match="ignored var"):
        inventory(NoAccess(), source_bucket="old", target_bucket="west", manifest_path=path)


def test_manifest_rejects_duplicate_json_fields_before_copy(tmp_path):
    from scripts.migrate_west_raw import copy_manifest, inventory

    client = Client()
    fixture_objects(client)
    path = tmp_path / "manifest.json"
    inventory(client, source_bucket="old", target_bucket="west", manifest_path=path)
    path.write_text(path.read_text().replace('"version": 1', '"version": 1, "version": 1'))
    with pytest.raises(ValueError, match="duplicate"):
        copy_manifest(client, path)
    assert client.buckets["west"].writes == 0

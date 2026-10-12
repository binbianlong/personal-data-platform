import base64
import json
import logging
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.integration.owntracks.test_owntracks_ingestion import RawStore

BUCKET = "test-owntracks"
SUBSCRIPTION = "projects/test/subscriptions/owntracks"
LOCATION = {"_type": "location", "tst": 1791504000, "lat": 35.0, "lon": 139.0, "t": "u"}


def receiver(store):
    from personal_data_platform.sources.owntracks.service import (
        OwnTracksConfig,
        create_receiver_app,
    )

    config = OwnTracksConfig("user", "private-password", "iphone", "a" * 64)
    return TestClient(create_receiver_app(config=config, repository=store))


def notification(store, **changes):
    key = next(iter(store.objects))
    attrs = {
        "eventType": "OBJECT_FINALIZE",
        "payloadFormat": "JSON_API_V1",
        "bucketId": BUCKET,
        "objectId": key,
        "objectGeneration": "12",
    }
    data = {
        "bucket": BUCKET,
        "name": key,
        "generation": "12",
        "timeCreated": "2026-10-09T00:00:00Z",
    }
    data.update(changes)
    return {
        "subscription": SUBSCRIPTION,
        "message": {
            "attributes": attrs,
            "data": base64.b64encode(json.dumps(data).encode()).decode(),
        },
    }


def setup_worker(tmp_path):
    from personal_data_platform.sources.owntracks.runtime import OwnTracksProcessor
    from personal_data_platform.sources.owntracks.service import create_worker_app

    store = RawStore(get_source("owntracks"))
    assert (
        receiver(store)
        .post("/webhooks/owntracks", json=LOCATION, auth=("user", "private-password"))
        .status_code
        == 200
    )
    database = str(tmp_path / "owntracks.duckdb")

    def factory():
        return Warehouse(connect(WarehouseConfig(database)))

    with closing(factory()) as warehouse:
        warehouse.migrate()
    processor = OwnTracksProcessor(repository=store, warehouse_factory=factory)
    client = TestClient(
        create_worker_app(processor=processor, subscription=SUBSCRIPTION, bucket=BUCKET)
    )
    return store, processor, client, factory


def test_receiver_authenticates_before_saving_and_retry_creates_one_raw():
    store = RawStore(get_source("owntracks"))
    client = receiver(store)
    assert client.post("/webhooks/owntracks", json=LOCATION).status_code == 401
    assert (
        client.post("/webhooks/owntracks", json=LOCATION, auth=("user", "wrong")).status_code == 401
    )
    assert not store.objects
    for _ in range(2):
        response = client.post(
            "/webhooks/owntracks",
            json=LOCATION,
            auth=("user", "private-password"),
            headers={"X-Limit-U": "user", "X-Limit-D": "iphone"},
        )
        assert response.status_code == 200
        assert response.json() == []
    assert len(store.objects) == 1


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"", 200),
        (b'{"_type":"transition"}', 200),
        (b"not-json", 400),
        (b" " * (1024 * 1024 + 1), 413),
    ],
)
def test_receiver_ignores_non_locations_and_rejects_bad_or_large_body(body, status):
    store = RawStore(get_source("owntracks"))
    response = receiver(store).post(
        "/webhooks/owntracks", content=body, auth=("user", "private-password")
    )
    assert response.status_code == status
    assert not store.objects


def test_receiver_rejects_other_device_without_saving():
    store = RawStore(get_source("owntracks"))
    response = receiver(store).post(
        "/webhooks/owntracks",
        json=LOCATION,
        auth=("user", "private-password"),
        headers={"X-Limit-D": "other-device"},
    )
    assert response.status_code == 403
    assert not store.objects


def test_storage_failure_is_retriable_and_logs_no_credentials_or_location(caplog):
    class BrokenStore:
        def put_compressed_raw(self, key, compressed):
            raise RuntimeError("private-password 35.0 139.0")

    with caplog.at_level(logging.ERROR):
        response = receiver(BrokenStore()).post(
            "/webhooks/owntracks", json=LOCATION, auth=("user", "private-password")
        )
    assert response.status_code == 503
    assert "private-password" not in caplog.text
    assert "139.0" not in caplog.text


def test_worker_commits_before_ack_and_redelivery_has_one_row(tmp_path):
    store, _, client, factory = setup_worker(tmp_path)
    for _ in range(2):
        assert client.post("/notifications/owntracks", json=notification(store)).status_code == 204
        with closing(factory()) as warehouse:
            assert warehouse.query_value("SELECT count(*) FROM base.owntracks_location") == 1
            assert warehouse.query_value("SELECT status FROM ops.ingestion_metadata") == "succeeded"
            assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0


@pytest.mark.parametrize(
    "field,value", [("bucket", "other"), ("name", "raw/fitbit/wrong"), ("generation", "13")]
)
def test_worker_rejects_mismatched_storage_metadata_without_writing(tmp_path, field, value):
    store, _, client, factory = setup_worker(tmp_path)
    assert (
        client.post(
            "/notifications/owntracks", json=notification(store, **{field: value})
        ).status_code
        == 400
    )
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.owntracks_location") == 0


def test_worker_lease_conflict_retries_and_preserves_competing_owner(tmp_path):
    store, _, client, factory = setup_worker(tmp_path)
    with closing(factory()) as warehouse:
        assert warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    assert client.post("/notifications/owntracks", json=notification(store)).status_code == 503
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "daily"
        warehouse.release_job_lock("loader", "daily")
    assert client.post("/notifications/owntracks", json=notification(store)).status_code == 204


def test_paused_worker_does_not_open_warehouse(tmp_path):
    store, processor, client, _ = setup_worker(tmp_path)
    processor.paused = True
    processor.warehouse_factory = lambda: pytest.fail("paused worker must not open database")
    assert client.post("/notifications/owntracks", json=notification(store)).status_code == 503


def test_worker_unknown_commit_retries_from_durable_success(tmp_path):
    store, processor, client, factory = setup_worker(tmp_path)

    def uncertain_factory():
        warehouse = factory()
        connection = warehouse.connection

        class Uncertain:
            commits = 0

            def execute(self, sql, *args, **kwargs):
                result = connection.execute(sql, *args, **kwargs)
                if sql == "COMMIT":
                    self.commits += 1
                    if self.commits == 2:
                        raise RuntimeError("commit response lost")
                return result

            def __getattr__(self, name):
                return getattr(connection, name)

        warehouse.connection = Uncertain()
        return warehouse

    processor.warehouse_factory = uncertain_factory
    assert client.post("/notifications/owntracks", json=notification(store)).status_code == 503
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.owntracks_location") == 1
        warehouse.connection.execute("UPDATE ops.job_lock SET expires_at=now()-INTERVAL '1 second'")
    processor.warehouse_factory = factory
    assert client.post("/notifications/owntracks", json=notification(store)).status_code == 204
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.owntracks_location") == 1

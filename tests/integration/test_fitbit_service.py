import logging
from datetime import UTC, datetime, timedelta

import duckdb
import pytest
from fastapi.testclient import TestClient

from personal_data_platform.sources.fitbit.models import Snapshot, Window
from personal_data_platform.sources.fitbit.receipts import GCSReceiptRepository, Receipt
from personal_data_platform.storage.motherduck import Warehouse
from tests.unit.test_fitbit_receipts import Client

NOW = datetime(2026, 9, 27, tzinfo=UTC)
WINDOW = Window("steps", NOW, NOW.replace(hour=1))


def _worker_setup(tmp_path, states):
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.service import ReceiptWorker

    database = str(tmp_path / "acquisition.duckdb")
    warehouse = Warehouse(duckdb.connect(database))
    warehouse.migrate()
    warehouse.close()
    receipts = GCSReceiptRepository(client=Client(), bucket="test")

    class Raw:
        def __init__(self):
            self.objects = {}
            self.put_count = 0
            self.head_count = 0

        def put_raw_object(self, key, content):
            self.put_count += 1
            raw = FitbitSource().parse_raw_key(
                key, storage_created_at=NOW, storage_generation=self.put_count
            )
            self.objects[key] = (raw, content)
            return raw

        def head_raw(self, key):
            self.head_count += 1
            value = self.objects.get(key)
            return value[0] if value else None

        def get_raw(self, key, *, generation):
            raw, content = self.objects[key]
            assert raw.storage_generation == generation
            return content

        def list_raw(self, prefix):
            raise AssertionError("normal acquisition must not list Raw")

    class API:
        def __init__(self):
            self.calls = []

        def fetch(self, window, *, subject_key):
            state = states[len(self.calls)]
            self.calls.append(window)
            return Snapshot(
                subject_key,
                window,
                NOW + timedelta(seconds=len(self.calls)),
                (),
                source_payload=({"state": state},),
            )

    raw = Raw()
    api = API()

    def factory():
        return Warehouse(duckdb.connect(database))

    worker = ReceiptWorker(
        receipts=receipts,
        repository=raw,
        client=api,
        warehouse_factory=factory,
        subject_key="self",
    )
    return receipts, raw, api, worker, factory


def test_worker_skips_consecutive_identical_complete_fetches(tmp_path):
    receipts, raw, api, worker, factory = _worker_setup(tmp_path, ["A", "A"])
    first = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    assert worker.run(first.receipt.key)
    assert worker.run(first.receipt.key)  # duplicate delivery after completion
    second = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    assert worker.run(second.receipt.key)
    assert len(api.calls) == 2
    assert raw.put_count == 1
    no_change = receipts.read(second.receipt.key).receipt.work[0]
    assert no_change.completed and no_change.raw is None
    assert no_change.fetched_at == NOW + timedelta(seconds=2)
    assert len(no_change.source_sha256 or "") == 64
    warehouse = factory()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    warehouse.close()


def test_worker_preserves_a_b_a_as_three_raw_observations(tmp_path):
    receipts, raw, api, worker, _ = _worker_setup(tmp_path, ["A", "B", "A"])
    for _ in range(3):
        stored = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
        assert worker.run(stored.receipt.key)
    assert len(api.calls) == 3
    assert raw.put_count == 3


def test_worker_recovers_after_db_intent_commit_before_raw_put(tmp_path):
    receipts, raw, api, worker, factory = _worker_setup(tmp_path, ["A", "B"])
    stored = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    original_put = raw.put_raw_object
    first = [True]

    def fail_before_put(key, content):
        if first[0]:
            first[0] = False
            raise OSError("interrupted before Raw upload")
        return original_put(key, content)

    raw.put_raw_object = fail_before_put
    with pytest.raises(OSError):
        worker.run(stored.receipt.key)
    warehouse = factory()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 1
    warehouse.close()

    assert worker.run(stored.receipt.key)
    assert len(api.calls) == 2 and raw.put_count == 1
    warehouse = factory()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    warehouse.close()


def test_repair_loads_orphan_when_receipt_expired(tmp_path):
    receipts, raw, api, worker, factory = _worker_setup(tmp_path, ["A"])
    stored = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    original_replace = receipts.replace

    def interrupted(current, receipt):
        if receipt.work[0].raw is not None:
            raise OSError("interrupted after Raw upload")
        return original_replace(current, receipt)

    receipts.replace = interrupted
    with pytest.raises(OSError):
        worker.run(stored.receipt.key)
    del receipts._bucket.values[stored.receipt.key]
    assert worker.recover_orphan_intents() == 1
    assert len(api.calls) == 1 and raw.put_count == 1
    warehouse = factory()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 1
    warehouse.close()


def test_repair_refetches_missing_orphan_with_newer_observation(tmp_path):
    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.sources.fitbit.adapter import FitbitSource

    receipts, raw, api, worker, factory = _worker_setup(tmp_path, ["A", "B"])
    stored = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))

    def interrupted(current, receipt):
        raise OSError("interrupted after Raw upload")

    receipts.replace = interrupted
    with pytest.raises(OSError):
        worker.run(stored.receipt.key)
    old_key, old_entry = next(iter(raw.objects.items()))
    raw.objects.clear()
    del receipts._bucket.values[stored.receipt.key]
    assert worker.recover_orphan_intents() == 1
    assert len(api.calls) == 2 and raw.put_count == 2
    warehouse = factory()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    current_digest = warehouse.query_value("SELECT source_sha256 FROM ops.fitbit_coverage")
    raw.objects[old_key] = old_entry
    assert run_loader_objects(raw, warehouse, [old_entry[0]], source=FitbitSource()).ok
    assert warehouse.query_value("SELECT source_sha256 FROM ops.fitbit_coverage") == current_digest
    warehouse.close()


@pytest.mark.parametrize("receipt_expired", [False, True])
def test_refetching_missing_raw_restores_healthy_audit(tmp_path, receipt_expired):
    from personal_data_platform.reconciliation.job import run_reconciliation
    from personal_data_platform.sources.fitbit.adapter import FitbitSource

    receipts, raw, api, worker, factory = _worker_setup(tmp_path, ["A", "B"])
    stored = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    original_get = raw.get_raw
    first = [True]

    def missing_once(key, *, generation):
        if first[0]:
            first[0] = False
            raw.objects.clear()
            raise FileNotFoundError("Raw disappeared before loading")
        return original_get(key, generation=generation)

    raw.get_raw = missing_once
    with pytest.raises(RuntimeError, match="Raw load failed"):
        worker.run(stored.receipt.key)
    assert receipts.read(stored.receipt.key).receipt.work[0].raw is not None

    warehouse = factory()
    assert (
        warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata WHERE status='failed'")
        == 1
    )
    warehouse.close()
    if receipt_expired:
        del receipts._bucket.values[stored.receipt.key]
        assert worker.recover_orphan_intents() == 1
    else:
        assert worker.run(stored.receipt.key)
    assert len(api.calls) == 2 and raw.put_count == 2
    warehouse = factory()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    # These marts are built by dbt in production; this test audits acquisition state.
    source = FitbitSource()
    for relation in source.required_relations:
        if relation.startswith("marts."):
            warehouse.connection.execute(f"CREATE VIEW {relation} AS SELECT 1 AS value")
    raw.list_raw = lambda prefix: [entry[0] for entry in raw.objects.values()]
    result = run_reconciliation(
        raw, warehouse, source=source, heartbeat=lambda _: None, now=NOW + timedelta(days=1)
    )
    assert result.ok
    assert result.failed_object_count == 0
    assert result.details["unrecoverable_uningested_object_count"] == 0
    warehouse.close()


def test_orphan_repair_is_not_blocked_by_older_pending_receipt(tmp_path):
    receipts, raw, api, worker, factory = _worker_setup(tmp_path, ["B"])
    pending = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    expired_key = "receipts/fitbit/v1/2026-09-27/" + "e" * 32 + ".json"
    warehouse = factory()
    for key, raw_key, fetched_at in (
        (pending.receipt.key, "pending-raw", NOW - timedelta(seconds=2)),
        (expired_key, "expired-raw", NOW - timedelta(seconds=1)),
    ):
        warehouse.connection.execute(
            "INSERT INTO ops.fitbit_raw_intent VALUES (?,?,?,?,?,?,?,?)",
            [key, 0, "self", "steps", WINDOW.start, WINDOW.end, raw_key, fetched_at],
        )
    warehouse.close()

    assert worker.recover_orphan_intents(limit=1) == 1
    assert len(api.calls) == 1 and raw.put_count == 1
    warehouse = factory()
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM ops.fitbit_raw_intent WHERE receipt_key=?", [expired_key]
        )
        == 0
    )
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM ops.fitbit_raw_intent WHERE receipt_key=?", [pending.receipt.key]
        )
        == 1
    )
    warehouse.close()


def test_receiver_keeps_receipt_on_queue_failure_and_requires_task_identity():
    from personal_data_platform.sources.fitbit.service import create_app
    from personal_data_platform.sources.fitbit.webhook import (
        AuthenticationError,
        VerifiedNotification,
    )

    receipts = GCSReceiptRepository(client=Client(), bucket="test")

    class Auth:
        def authenticate(self, **kwargs):
            return VerifiedNotification("self", (WINDOW,))

    class Identity:
        def authenticate(self, authorization):
            raise AuthenticationError("invalid")

    class Queue:
        def enqueue(self, key):
            raise OSError("queue failed")

    class Worker:
        def run(self, key):
            raise AssertionError("unauthorized task executed")

    client = TestClient(
        create_app(
            authenticator=Auth(),
            identity=Identity(),
            receipts=receipts,
            queue=Queue(),
            worker=Worker(),
        )
    )
    assert client.post("/webhooks/fitbit", content=b"{}").status_code == 503
    assert len(receipts.inventory().pending) == 1
    assert client.post("/internal/tasks/fitbit", json={"receipt_key": "x"}).status_code == 401


def test_worker_reuses_saved_raw_after_db_commit_before_receipt_failure(tmp_path):
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.service import ReceiptWorker

    database = str(tmp_path / "worker.duckdb")
    warehouse = Warehouse(duckdb.connect(database))
    warehouse.migrate()
    warehouse.close()
    receipts = GCSReceiptRepository(client=Client(), bucket="test")
    stored = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    reads = []
    fetches = []

    class Raw:
        def put_raw_object(self, key, content):
            self.key, self.content = key, content
            return FitbitSource().parse_raw_key(key, storage_created_at=NOW, storage_generation=1)

        def get_raw(self, key, *, generation):
            reads.append(key)
            return self.content

        def list_raw(self, prefix):
            raise AssertionError("worker listed storage")

    class API:
        def fetch(self, window, *, subject_key):
            fetches.append(window)
            return Snapshot(subject_key, window, NOW, ())

    original_replace = receipts.replace
    fail = [True]

    def save(stored, receipt):
        if receipt.work[0].completed and fail[0]:
            fail[0] = False
            raise OSError("receipt write failed after DB commit")
        return original_replace(stored, receipt)

    receipts.replace = save
    worker = ReceiptWorker(
        receipts=receipts,
        repository=Raw(),
        client=API(),
        warehouse_factory=lambda: Warehouse(duckdb.connect(database)),
        subject_key="self",
    )
    with pytest.raises(OSError):
        worker.run(stored.receipt.key)
    assert worker.run(stored.receipt.key)
    assert len(fetches) == 1 and len(reads) == 1
    assert receipts.read(stored.receipt.key).receipt.completed_at is not None


def test_worker_pause_and_shared_lock_are_retryable(tmp_path):
    from personal_data_platform.loader.job import JobAlreadyRunning
    from personal_data_platform.sources.fitbit.service import ProcessingPaused, ReceiptWorker

    receipts = GCSReceiptRepository(client=Client(), bucket="test")
    stored = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    database = str(tmp_path / "locked.duckdb")
    warehouse = Warehouse(duckdb.connect(database))
    warehouse.migrate()
    warehouse.acquire_job_lock("loader", "screen-time", lease_seconds=7500)
    warehouse.close()
    worker = ReceiptWorker(
        receipts=receipts,
        repository=None,
        client=None,
        warehouse_factory=lambda: Warehouse(duckdb.connect(database)),
        subject_key="self",
        paused=True,
    )
    with pytest.raises(ProcessingPaused):
        worker.run(stored.receipt.key)
    worker.paused = False
    with pytest.raises(JobAlreadyRunning):
        worker.run(stored.receipt.key)


@pytest.mark.parametrize("paused", [True, False])
def test_task_acknowledges_pause_but_retries_failure_and_preserves_receipt(paused, caplog):
    from personal_data_platform.sources.fitbit.runtime import repair_receipts
    from personal_data_platform.sources.fitbit.service import ReceiptWorker, create_app

    receipts = GCSReceiptRepository(client=Client(), bucket="test")
    stored = receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))

    class Identity:
        def authenticate(self, authorization):
            assert authorization == "Bearer test"

    class Queue:
        def __init__(self):
            self.keys = []

        def enqueue(self, key):
            self.keys.append(key)

    def unavailable_warehouse():
        if paused:
            pytest.fail("paused tasks must not connect to the warehouse")
        raise OSError("warehouse unavailable")

    queue = Queue()
    worker = ReceiptWorker(
        receipts=receipts,
        repository=None,
        client=None,
        warehouse_factory=unavailable_warehouse,
        subject_key="self",
        paused=paused,
    )
    client = TestClient(
        create_app(
            authenticator=None,
            identity=Identity(),
            receipts=receipts,
            queue=queue,
            worker=worker,
        )
    )
    with caplog.at_level(logging.INFO):
        response = client.post(
            "/internal/tasks/fitbit",
            headers={"Authorization": "Bearer test"},
            json={"receipt_key": stored.receipt.key},
        )
    assert response.status_code == (204 if paused else 503)
    assert any(record.levelno >= logging.ERROR for record in caplog.records) is not paused
    assert receipts.read(stored.receipt.key) == stored
    assert queue.keys == []
    from personal_data_platform.sources.fitbit.api import SyncTime
    from personal_data_platform.sources.fitbit.sync_state import GCSFitbitSyncState

    class Devices:
        def latest_tracker_sync(self):
            return SyncTime.from_datetime(NOW)

    repair_receipts(
        receipts,
        queue,
        subject_key="self",
        now=NOW,
        paused=False,
        sync_store=GCSFitbitSyncState(client=Client(), bucket="test"),
        device_client=Devices(),
    )
    assert stored.receipt.key in queue.keys


def test_receiver_requires_all_publishes_before_204():
    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import Verification, VerifiedNotification

    class Auth:
        def authenticate(self, **kwargs):
            if kwargs["body"] == b'{"type":"verification"}':
                return Verification()
            return VerifiedNotification("self", (WINDOW,), groups=((WINDOW,), (WINDOW,)))

    class Publisher:
        def __init__(self):
            self.calls = 0
            self.fail = True

        def publish(self, notification):
            self.calls += 1
            if self.fail and self.calls == 2:
                raise TimeoutError("unknown publish result")
            return "message"

    publisher = Publisher()
    client = TestClient(create_pubsub_app(authenticator=Auth(), notifications=publisher))
    assert client.post("/webhooks/fitbit", content="{}").status_code == 503
    assert publisher.calls == 2
    publisher.fail = False
    assert client.post("/webhooks/fitbit", content="{}").status_code == 204
    assert client.post("/webhooks/fitbit", content='{"type":"verification"}').status_code == 201
    assert publisher.calls == 4
    assert client.post("/internal/tasks/fitbit", content="{}").status_code == 404


@pytest.mark.parametrize("groups", [1, 2])
def test_receiver_rejects_expansion_before_publishing(groups):
    from datetime import timedelta

    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import VerifiedNotification

    start = datetime(2020, 1, 1, tzinfo=UTC)
    days = 1001 if groups == 1 else 600
    window = Window("daily-resting-heart-rate", start, start + timedelta(days=days))

    class Auth:
        def authenticate(self, **kwargs):
            return VerifiedNotification("self", (window,), groups=((window,),) * groups)

    class Publisher:
        def __init__(self):
            self.published = []

        def publish(self, notification):
            self.published.append(notification)
            return "message"

    publisher = Publisher()
    response = TestClient(create_pubsub_app(authenticator=Auth(), notifications=publisher)).post(
        "/webhooks/fitbit", content="{}"
    )
    assert response.status_code == 400
    assert publisher.published == []


def test_partial_publish_returns_503_and_retry_preserves_units():
    from datetime import timedelta

    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import VerifiedNotification

    start = datetime(2026, 10, 1, 15, tzinfo=UTC)
    window = Window("steps", start, start + timedelta(days=3))

    class Auth:
        def authenticate(self, **kwargs):
            return VerifiedNotification("self", (window,))

    class Publisher:
        def __init__(self):
            self.published = []

        def publish(self, notification):
            self.published.append(notification)
            if len(self.published) == 2:
                raise TimeoutError("unknown publish result")
            return "message"

    publisher = Publisher()
    client = TestClient(create_pubsub_app(authenticator=Auth(), notifications=publisher))
    assert client.post("/webhooks/fitbit", content="{}").status_code == 503
    assert client.post("/webhooks/fitbit", content="{}").status_code == 204
    assert len(publisher.published) == 5
    assert [n.windows for n in publisher.published[:2]] == [
        n.windows for n in publisher.published[2:4]
    ]
    assert [n.windows[0] for n in publisher.published[2:]] == [
        Window("steps", start, start + timedelta(days=1)),
        Window("steps", start + timedelta(days=1), start + timedelta(days=2)),
        Window("steps", start + timedelta(days=2), start + timedelta(days=3)),
    ]

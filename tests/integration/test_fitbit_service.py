from datetime import UTC, datetime

import duckdb
import pytest
from fastapi.testclient import TestClient

from personal_data_platform.sources.fitbit.models import Snapshot, Window
from personal_data_platform.sources.fitbit.receipts import GCSReceiptRepository, Receipt
from personal_data_platform.storage.motherduck import Warehouse
from tests.unit.test_fitbit_receipts import Client

NOW = datetime(2026, 9, 27, tzinfo=UTC)
WINDOW = Window("steps", NOW, NOW.replace(hour=1))


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

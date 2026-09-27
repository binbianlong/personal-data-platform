from datetime import UTC, datetime, timedelta

from personal_data_platform.sources.fitbit.models import Window
from personal_data_platform.sources.fitbit.receipts import GCSReceiptRepository, Receipt
from tests.unit.test_fitbit_receipts import Client


def test_repair_requeues_pending_and_creates_one_daily_sweep():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    now = datetime(2026, 9, 27, tzinfo=UTC)
    receipts = GCSReceiptRepository(client=Client(), bucket="test")
    old = receipts.create(
        Receipt.create(
            "self",
            (Window("steps", now - timedelta(days=28), now - timedelta(days=27)),),
            received_at=now - timedelta(days=28),
        )
    )

    class Queue:
        keys = []

        def enqueue(self, key):
            self.keys.append(key)

    queue = Queue()
    summary = repair_receipts(receipts, queue, subject_key="self", now=now)
    assert summary.at_risk_count == 1
    assert old.receipt.key in queue.keys
    assert summary.pending_count == 2
    again = repair_receipts(receipts, queue, subject_key="self", now=now)
    assert again.pending_count == 2
    assert len(receipts.inventory().pending) == 2


def test_disabled_repair_requires_no_cloud_configuration(monkeypatch):
    from personal_data_platform.sources.fitbit.runtime import run_repair_from_env

    monkeypatch.delenv("PDP_FITBIT_REPAIR_ENABLED", raising=False)
    assert run_repair_from_env().enabled is False


def test_twice_daily_runs_share_the_tokyo_date_receipt():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    receipts = GCSReceiptRepository(client=Client(), bucket="test")

    class Queue:
        def enqueue(self, key):
            pass

    for now in (
        datetime(2026, 9, 26, 19, 30, tzinfo=UTC),
        datetime(2026, 9, 27, 7, 30, tzinfo=UTC),
    ):
        repair_receipts(receipts, Queue(), subject_key="self", now=now)
    assert len(receipts.inventory().pending) == 1

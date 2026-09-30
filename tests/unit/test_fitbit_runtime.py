"""Scheduled Fitbit checks use a durable cursor and bounded daily receipts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from personal_data_platform.sources.fitbit.api import SyncTime
from personal_data_platform.sources.fitbit.models import Window
from personal_data_platform.sources.fitbit.receipts import GCSReceiptRepository, Receipt
from personal_data_platform.sources.fitbit.sync_state import GCSFitbitSyncState, SyncState
from tests.unit.test_fitbit_receipts import Client


class Queue:
    def __init__(self):
        self.keys: list[str] = []

    def enqueue(self, key: str) -> None:
        self.keys.append(key)


class Devices:
    def __init__(self, latest: SyncTime | None):
        self.latest = latest
        self.calls = 0

    def latest_tracker_sync(self) -> SyncTime | None:
        self.calls += 1
        return self.latest


def _stores():
    client = Client()
    return (
        GCSReceiptRepository(client=client, bucket="test"),
        GCSFitbitSyncState(client=client, bucket="test"),
    )


def _complete(receipts: GCSReceiptRepository, key: str, at: datetime) -> None:
    stored = receipts.read(key)
    work = tuple(replace(item, completed=True) for item in stored.receipt.work)
    receipts.replace(stored, replace(stored.receipt, work=work, completed_at=at))


def test_first_repair_bootstraps_prior_seven_completed_tokyo_days_once():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    now = datetime(2026, 9, 28, 3, tzinfo=UTC)
    receipts, state = _stores()
    queue = Queue()
    devices = Devices(SyncTime.from_datetime(now))
    first = repair_receipts(
        receipts, queue, subject_key="self", now=now, sync_store=state, device_client=devices
    )
    assert first.pending_count == 1
    assert devices.calls == 0
    assert len(receipts.inventory().pending[0].receipt.work) == 35
    assert state.read("self").state.bootstrap_day.isoformat() == "2026-09-28"
    second = repair_receipts(
        receipts, queue, subject_key="self", now=now, sync_store=state, device_client=devices
    )
    assert second.pending_count == 1


def test_scheduled_repair_accepts_expanded_interval_work():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    now = datetime(2026, 9, 28, 3, tzinfo=UTC)
    receipts, state = _stores()
    devices = Devices(SyncTime.from_datetime(now))
    repair_receipts(
        receipts,
        Queue(),
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=devices,
    )
    stored = receipts.inventory().pending[0]
    work = list(stored.receipt.work)
    original = work[0].window
    work[0] = replace(
        work[0],
        window=Window(
            original.data_type,
            original.start - timedelta(minutes=1),
            original.end + timedelta(minutes=1),
        ),
    )
    receipts.replace(stored, replace(stored.receipt, work=tuple(work)))

    assert (
        repair_receipts(
            receipts,
            Queue(),
            subject_key="self",
            now=now,
            sync_store=state,
            device_client=devices,
        ).pending_count
        == 1
    )


def test_completed_bootstrap_starts_same_day_device_sync_without_weekly_duplicate():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    now = datetime(2026, 9, 28, 3, tzinfo=UTC)
    receipts, state = _stores()
    queue = Queue()
    devices = Devices(SyncTime.from_datetime(now))
    repair_receipts(
        receipts, queue, subject_key="self", now=now, sync_store=state, device_client=devices
    )
    bootstrap = receipts.inventory().pending[0].receipt
    _complete(receipts, bootstrap.key, now)
    summary = repair_receipts(
        receipts, queue, subject_key="self", now=now, sync_store=state, device_client=devices
    )
    assert summary.pending_count == 1
    pending = receipts.inventory().pending[0].receipt
    assert pending.origin == "device-sync"
    assert not any(value.receipt.origin == "weekly" for value in receipts.inventory().pending)
    assert state.read("self").state.last_completed_sync.text == "2026-09-27T15:00:00Z"


def test_device_gap_issues_at_most_ninety_days_without_advancing_cursor():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    now = datetime(2026, 9, 28, 3, tzinfo=UTC)
    receipts, state = _stores()
    prior = SyncTime.parse("2026-06-01T00:00:00Z")
    state.replace(
        state.read("self"),
        SyncState(
            subject_key="self",
            bootstrap_day=now.date(),
            bootstrap_complete=True,
            last_completed_sync=prior,
            weekly_completed="2026-W40",
        ),
    )
    queue = Queue()
    target = SyncTime.from_datetime(now)
    summary = repair_receipts(
        receipts,
        queue,
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=Devices(target),
    )
    assert summary.pending_count == 90
    assert state.read("self").state.last_completed_sync == prior
    assert state.read("self").state.device_target == target
    for item in receipts.inventory().pending:
        _complete(receipts, item.receipt.key, now)
    repair_receipts(
        receipts,
        queue,
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=Devices(target),
    )
    assert state.read("self").state.last_completed_sync == prior
    remaining = repair_receipts(
        receipts,
        queue,
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=Devices(target),
    )
    assert 0 < remaining.pending_count <= 90
    for item in receipts.inventory().pending:
        _complete(receipts, item.receipt.key, now)
    repair_receipts(
        receipts,
        queue,
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=Devices(target),
    )
    assert state.read("self").state.last_completed_sync == target


def test_same_day_tracker_resync_waits_for_completed_receipt_and_gets_new_key():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    now = datetime(2026, 9, 28, 4, tzinfo=UTC)
    receipts, state = _stores()
    prior = SyncTime.parse("2026-09-28T00:00:00.123456789Z")
    first_sync = SyncTime.parse("2026-09-28T02:00:00.123456789Z")
    state.replace(
        state.read("self"),
        SyncState(
            subject_key="self",
            bootstrap_day=now.date(),
            bootstrap_complete=True,
            last_completed_sync=prior,
            weekly_completed="2026-W40",
        ),
    )
    devices = Devices(first_sync)
    repair_receipts(
        receipts,
        Queue(),
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=devices,
    )
    first = receipts.inventory().pending[0].receipt
    assert first.origin == "device-sync"
    assert (
        max(item.window.end for item in first.work if item.window.data_type == "steps")
        > first_sync.utc_second
    )
    assert state.read("self").state.last_completed_sync == prior
    _complete(receipts, first.key, now)
    repair_receipts(
        receipts,
        Queue(),
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=devices,
    )
    assert state.read("self").state.last_completed_sync == first_sync

    devices.latest = SyncTime.parse("2026-09-28T02:30:00.000000001Z")
    repair_receipts(
        receipts,
        Queue(),
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=devices,
    )
    second = receipts.inventory().pending[0].receipt
    assert second.key != first.key


def test_paused_repair_creates_no_scheduled_receipts_or_api_calls():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    now = datetime(2026, 9, 28, 3, tzinfo=UTC)
    receipts, state = _stores()
    old = receipts.create(
        Receipt.create(
            "self",
            (Window("steps", now - timedelta(days=88), now - timedelta(days=87)),),
            received_at=now - timedelta(days=88),
        )
    )
    queue = Queue()
    devices = Devices(SyncTime.from_datetime(now))
    result = repair_receipts(
        receipts,
        queue,
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=devices,
        paused=True,
    )
    assert result.pending_count == result.at_risk_count == 1
    assert old.receipt.key not in queue.keys
    assert devices.calls == 0
    assert state.read("self").generation == 0


def test_weekly_receipt_still_runs_when_device_list_is_empty():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    now = datetime(2026, 10, 5, 3, tzinfo=UTC)
    receipts, state = _stores()
    baseline = SyncTime.from_datetime(now - timedelta(days=7))
    state.replace(
        state.read("self"),
        SyncState(
            subject_key="self",
            bootstrap_day=(now - timedelta(days=7)).date(),
            bootstrap_complete=True,
            last_completed_sync=baseline,
            weekly_completed="2026-W40",
        ),
    )
    summary = repair_receipts(
        receipts,
        Queue(),
        subject_key="self",
        now=now,
        sync_store=state,
        device_client=Devices(None),
    )
    assert summary.failed_count == 1
    assert any(item.receipt.origin == "weekly" for item in receipts.inventory().pending)


def test_weekly_receipts_have_distinct_keys_when_first_check_is_tuesday():
    from personal_data_platform.sources.fitbit.runtime import repair_receipts

    first_check = datetime(2026, 10, 6, 3, tzinfo=UTC)
    next_check = datetime(2026, 10, 12, 3, tzinfo=UTC)
    receipts, state = _stores()
    state.replace(
        state.read("self"),
        SyncState(
            subject_key="self",
            bootstrap_day=first_check.date(),
            bootstrap_complete=True,
            last_completed_sync=SyncTime.from_datetime(first_check),
            weekly_completed="2026-W40",
        ),
    )
    devices = Devices(None)
    repair_receipts(
        receipts,
        Queue(),
        subject_key="self",
        now=first_check,
        sync_store=state,
        device_client=devices,
    )
    first = next(
        item.receipt for item in receipts.inventory().pending if item.receipt.origin == "weekly"
    )
    _complete(receipts, first.key, first_check)
    repair_receipts(
        receipts,
        Queue(),
        subject_key="self",
        now=first_check,
        sync_store=state,
        device_client=devices,
    )
    repair_receipts(
        receipts,
        Queue(),
        subject_key="self",
        now=next_check,
        sync_store=state,
        device_client=devices,
    )
    second = next(
        item.receipt for item in receipts.inventory().pending if item.receipt.origin == "weekly"
    )
    assert second.key != first.key
    assert state.read("self").state.weekly_pending == "2026-W42"

"""Repair composition preserves real warehouse leases and observable outcomes."""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient

from personal_data_platform.cli import main
from personal_data_platform.sources.fitbit import runtime
from personal_data_platform.sources.fitbit.adapter import FitbitSource
from personal_data_platform.sources.fitbit.models import Snapshot, Window
from personal_data_platform.sources.fitbit.receipts import Receipt, ReceiptReadConflict
from personal_data_platform.sources.fitbit.service import ReceiptWorker, create_app
from personal_data_platform.storage.motherduck import Warehouse
from tests.unit.test_fitbit_runtime import Devices, Queue, _stores

NOW = datetime(2026, 9, 28, 3, tzinfo=UTC)
WINDOW = Window("steps", NOW.replace(hour=0), NOW.replace(hour=1))


@pytest.fixture
def repair_env(monkeypatch, tmp_path, caplog):
    database = str(tmp_path / "repair.duckdb")
    warehouse = Warehouse(duckdb.connect(database))
    warehouse.migrate()
    for relation in FitbitSource.required_relations:
        if relation.startswith("marts."):
            warehouse.connection.execute(f"CREATE VIEW {relation} AS SELECT 1 AS value")
    warehouse.close()

    def factory():
        return Warehouse(duckdb.connect(database))

    receipts, sync_state = _stores()
    queue = Queue()
    raw = SimpleNamespace(objects=[])
    raw.list_raw = lambda prefix: raw.objects
    for key, value in {
        "PDP_FITBIT_REPAIR_ENABLED": "true",
        "PDP_FITBIT_PROCESSING_PAUSED": "false",
        "PDP_FITBIT_SUBJECT_KEY": "self",
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "GCS_BUCKET": "test-bucket",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runtime, "_stores", lambda: (receipts, raw))
    monkeypatch.setattr(runtime, "_queue", lambda: queue)
    monkeypatch.setattr(runtime, "_warehouse", factory)
    monkeypatch.setattr(runtime.storage, "Client", lambda **kwargs: receipts._client)
    monkeypatch.setattr(runtime.GoogleOAuth, "from_env", lambda: object())
    monkeypatch.setattr(runtime, "HealthClient", lambda **kwargs: Devices(None))

    logger = logging.getLogger("personal_data_platform.sources.fitbit")
    previous = (logger.level, logger.propagate, logger.handlers[:])
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(caplog.handler)
    yield SimpleNamespace(
        warehouse=factory, receipts=receipts, state=sync_state, queue=queue, raw=raw
    )
    for handler in logger.handlers[:]:
        if handler not in previous[2]:
            logger.removeHandler(handler)
            if handler is not caplog.handler:
                handler.close()
    logger.setLevel(previous[0])
    logger.propagate = previous[1]


@pytest.mark.parametrize(
    ("lease", "missing_raw", "phases"),
    [
        ("loader", False, ("orphan_recovery",)),
        ("reconciliation", False, ("raw_audit",)),
        ("loader", True, ("raw_audit", "orphan_recovery")),
    ],
)
def test_repair_defers_busy_leases_without_releasing_another_owner(
    repair_env, caplog, lease, missing_raw, phases
):
    if missing_raw:
        from personal_data_platform.sources.fitbit.raw import encode_snapshot

        key, _ = encode_snapshot(Snapshot("self", WINDOW, NOW, ()))
        repair_env.raw.objects.append(
            FitbitSource().parse_raw_key(key, storage_created_at=NOW, storage_generation=1)
        )
    warehouse = repair_env.warehouse()
    assert warehouse.acquire_job_lock(lease, "other", lease_seconds=3600)
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    warehouse.close()

    summary = runtime.run_repair_from_env()
    assert summary.status == "deferred"
    assert summary.deferred_phases == phases
    assert summary.failed_count == summary.at_risk_count == 0
    assert summary.queued_count == 1
    assert not any(getattr(record, "status", None) == "succeeded" for record in caplog.records)
    warehouse = repair_env.warehouse()
    assert (
        warehouse.query_value("SELECT owner_id FROM ops.job_lock WHERE job_name=?", [lease])
        == "other"
    )
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    warehouse.release_job_lock(lease, "other")
    warehouse.close()


def test_repair_completion_emits_one_success_even_with_async_pending_receipts(repair_env, caplog):
    summary = runtime.run_repair_from_env()
    assert summary.status == "succeeded"
    assert summary.pending_count == summary.queued_count == 1
    events = [
        record for record in caplog.records if getattr(record, "event", None) == "fitbit_repair"
    ]
    assert len(events) == 1
    assert events[0].status == "succeeded"
    warehouse = repair_env.warehouse()
    assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    warehouse.close()


@pytest.mark.parametrize("failure", ["enqueue", "audit", "retention"])
def test_repair_real_failures_take_priority_over_deferral(repair_env, monkeypatch, failure, capsys):
    if failure == "enqueue":

        def broken_enqueue(key):
            raise OSError("queue unavailable")

        monkeypatch.setattr(repair_env.queue, "enqueue", broken_enqueue)
    elif failure == "audit":
        warehouse = repair_env.warehouse()
        warehouse.connection.execute("DROP VIEW marts.daily_fitbit_health")
        warehouse.close()
    else:
        from datetime import timedelta

        repair_env.receipts.create(
            Receipt.create("self", (WINDOW,), received_at=datetime.now(UTC) - timedelta(days=88))
        )
    warehouse = repair_env.warehouse()
    assert warehouse.acquire_job_lock("loader", "other", lease_seconds=3600)
    warehouse.close()
    assert main(["fitbit", "repair"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "failed"
    assert summary["deferred_phases"] == ["orphan_recovery"]


def test_repair_cli_exposes_deferral_and_recovery(repair_env, capsys):
    warehouse = repair_env.warehouse()
    warehouse.acquire_job_lock("loader", "other", lease_seconds=3600)
    warehouse.close()
    assert main(["fitbit", "repair"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "deferred"
    assert summary["deferred_phases"] == ["orphan_recovery"]
    assert summary["receipt_read_deferred_count"] == 0
    warehouse = repair_env.warehouse()
    warehouse.release_job_lock("loader", "other")
    warehouse.close()
    assert main(["fitbit", "repair"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"


@pytest.mark.parametrize("phase", ["scheduled_receipts", "receipt_inventory"])
def test_receipt_deferral_still_enqueues_unaffected_work(repair_env, monkeypatch, phase, caplog):
    unaffected = repair_env.receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    if phase == "scheduled_receipts":

        def changing_read(key):
            raise ReceiptReadConflict("receipt keeps changing")

        monkeypatch.setattr(repair_env.receipts, "read", changing_read)
    else:
        inventory = repair_env.receipts.inventory
        monkeypatch.setattr(
            repair_env.receipts, "inventory", lambda: replace(inventory(), deferred_count=1)
        )
    summary = runtime.run_repair_from_env()
    assert unaffected.receipt.key in repair_env.queue.keys
    assert summary.status == "deferred"
    assert phase in summary.deferred_phases
    assert summary.receipt_read_deferred_count == (phase == "receipt_inventory")
    assert repair_env.state.read("self").state.bootstrap_complete is False
    assert not any(getattr(record, "status", None) == "succeeded" for record in caplog.records)


@pytest.mark.parametrize(
    ("enabled", "paused", "status"), [(False, False, "disabled"), (True, True, "paused")]
)
def test_inactive_repair_does_not_report_completion(
    repair_env, monkeypatch, caplog, enabled, paused, status
):
    monkeypatch.setenv("PDP_FITBIT_REPAIR_ENABLED", str(enabled).lower())
    monkeypatch.setenv("PDP_FITBIT_PROCESSING_PAUSED", str(paused).lower())
    summary = runtime.run_repair_from_env()
    assert summary.status == status
    assert repair_env.queue.keys == []
    assert not any(getattr(record, "status", None) == "succeeded" for record in caplog.records)


def test_orphan_storage_failure_remains_a_failed_cli_execution(
    repair_env, monkeypatch, capsys, caplog
):
    def failed_recovery(self, *, limit):
        raise OSError("storage unavailable")

    monkeypatch.setattr(ReceiptWorker, "recover_orphan_intents", failed_recovery)
    assert main(["fitbit", "repair"]) == 1
    assert "storage unavailable" in capsys.readouterr().err
    events = [
        record for record in caplog.records if getattr(record, "event", None) == "fitbit_repair"
    ]
    assert len(events) == 1
    assert events[0].status == "failed"
    assert events[0].levelno == logging.ERROR


def test_worker_lease_contention_retries_without_failure_alert(repair_env, caplog):
    stored = repair_env.receipts.create(Receipt.create("self", (WINDOW,), received_at=NOW))
    warehouse = repair_env.warehouse()
    warehouse.acquire_job_lock("loader", "other", lease_seconds=3600)
    warehouse.close()
    app = create_app(
        authenticator=None,
        identity=SimpleNamespace(authenticate=lambda authorization: None),
        receipts=repair_env.receipts,
        queue=repair_env.queue,
        worker=ReceiptWorker(
            receipts=repair_env.receipts,
            repository=repair_env.raw,
            client=None,
            warehouse_factory=repair_env.warehouse,
            subject_key="self",
        ),
    )
    response = TestClient(app).post(
        "/internal/tasks/fitbit",
        headers={"Authorization": "Bearer test"},
        json={"receipt_key": stored.receipt.key},
    )
    assert response.status_code == 503
    assert repair_env.receipts.read(stored.receipt.key) == stored
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert any("deferred" in record.message for record in caplog.records)


@pytest.fixture
def cursor_env(monkeypatch, tmp_path):
    database = str(tmp_path / "cursor.duckdb")
    warehouse = Warehouse(duckdb.connect(database))
    warehouse.migrate()
    warehouse.close()
    monkeypatch.setenv("PDP_FITBIT_DELIVERY_MODE", "pubsub")
    monkeypatch.setenv("PDP_FITBIT_SUBJECT_KEY", "self")
    monkeypatch.setenv("PDP_FITBIT_PROCESSING_PAUSED", "false")
    monkeypatch.setattr(runtime, "_warehouse", lambda: Warehouse(duckdb.connect(database)))
    calls = []
    outcomes = []

    class Runner:
        subject_key = "self"
        client = Devices(runtime.SyncTime.parse("2026-09-28T01:02:03.123456789Z"))

        def run_windows(self, windows, *, warehouse, lease_owner, timeout_seconds):
            warehouse.require_job_lock(lease_owner)
            calls.extend(windows)
            return (
                outcomes.pop(0)
                if outcomes
                else SimpleNamespace(
                    ok=True, completed_scopes=1, failed_scopes=0, deferred_scopes=0
                )
            )

    monkeypatch.setattr(runtime, "_acquisition_runner", Runner, raising=False)
    yield SimpleNamespace(database=database, calls=calls, outcomes=outcomes)


def test_partial_failure_cursor_resumes_after_committed_day_type(cursor_env, monkeypatch):
    cursor_env.outcomes.extend(
        [
            SimpleNamespace(ok=True, completed_scopes=1, failed_scopes=0, deferred_scopes=0),
            SimpleNamespace(ok=False, completed_scopes=0, failed_scopes=1, deferred_scopes=0),
        ]
    )
    start = NOW.replace(day=1, hour=0)
    end = NOW.replace(day=4, hour=0)
    assert (
        runtime.run_sync_from_env(
            start=start, end=end, data_types=("heart-rate",), resume_id="backfill"
        )
        == 1
    )
    with duckdb.connect(cursor_env.database) as connection:
        assert (
            connection.execute("SELECT next_start FROM ops.fitbit_repair_cursor").fetchone()[0]
            > start
        )
    resumed_at = cursor_env.calls[-1].start
    cursor_env.calls.clear()
    assert runtime.run_sync_from_env(start=None, end=None, resume_id="backfill") == 0
    assert cursor_env.calls[0].start == resumed_at


def test_wide_backfill_stops_at_scope_limit_without_completing_cursor(cursor_env, monkeypatch):
    monkeypatch.setenv("PDP_FITBIT_MAX_SCOPES", "2")
    assert (
        runtime.run_sync_from_env(
            start=NOW.replace(day=1), end=NOW, data_types=("heart-rate",), resume_id="wide"
        )
        == 1
    )
    assert len(cursor_env.calls) == 2
    with duckdb.connect(cursor_env.database) as connection:
        next_start, end = connection.execute(
            "SELECT next_start, range_end FROM ops.fitbit_repair_cursor"
        ).fetchone()
        assert next_start < end


def test_daily_repair_preserves_older_gap_and_nanosecond_sync(cursor_env):
    warehouse = Warehouse(duckdb.connect(cursor_env.database))
    warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    warehouse.connection.execute(
        "INSERT INTO ops.fitbit_repair_cursor VALUES "
        "('daily:self:heart-rate','self',?, ?, ['heart-rate'],?)",
        [NOW.replace(day=1), NOW.replace(day=2), NOW],
    )
    warehouse.close()
    summary = runtime.run_daily_repair(now=NOW, lease_owner="daily", timeout_seconds=6000)
    assert summary.ok
    assert min(window.start for window in cursor_env.calls) == NOW.replace(day=1)
    with duckdb.connect(cursor_env.database) as connection:
        assert connection.execute("SELECT last_sync_time FROM ops.fitbit_device_sync").fetchone()[
            0
        ] == ("2026-09-28T01:02:03.123456789Z")


@pytest.mark.parametrize("outcome", ["paused", "deferred", "failed"])
def test_daily_incomplete_repair_does_not_advance_cursor(cursor_env, monkeypatch, outcome):
    warehouse = Warehouse(duckdb.connect(cursor_env.database))
    warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    warehouse.close()
    if outcome == "paused":
        monkeypatch.setenv("PDP_FITBIT_PROCESSING_PAUSED", "true")
    else:
        cursor_env.outcomes.append(
            SimpleNamespace(
                ok=False,
                completed_scopes=0,
                failed_scopes=int(outcome == "failed"),
                deferred_scopes=int(outcome == "deferred"),
            )
        )
    summary = runtime.run_daily_repair(now=NOW, lease_owner="daily", timeout_seconds=6000)
    assert not summary.ok
    assert summary.failed_scopes == int(outcome == "failed")
    if outcome != "paused":
        with duckdb.connect(cursor_env.database) as connection:
            first = cursor_env.calls[0]
            assert (
                connection.execute(
                    "SELECT next_start FROM ops.fitbit_repair_cursor "
                    "WHERE list_contains(data_types, ?)",
                    [first.data_type],
                ).fetchone()[0]
                == first.start
            )


def test_sync_deadline_does_not_advance_committed_cursor_after_budget(cursor_env, monkeypatch):
    points = iter([0, 0, 3001])
    monkeypatch.setattr(runtime.monotonic_time, "monotonic", lambda: next(points))
    assert (
        runtime.run_sync_from_env(
            start=NOW.replace(day=1),
            end=NOW.replace(day=3),
            data_types=("heart-rate",),
            resume_id="deadline",
        )
        == 1
    )
    with duckdb.connect(cursor_env.database) as connection:
        assert connection.execute("SELECT next_start FROM ops.fitbit_repair_cursor").fetchone()[
            0
        ] == NOW.replace(day=1)


def test_daily_repair_resumes_unfinished_manual_backfill(cursor_env):
    warehouse = Warehouse(duckdb.connect(cursor_env.database))
    warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    warehouse.connection.execute(
        "INSERT INTO ops.fitbit_repair_cursor VALUES "
        "('manual:heart-rate','self',?, ?, ['heart-rate'],?)",
        [NOW.replace(day=1), NOW.replace(day=3), NOW],
    )
    warehouse.close()
    summary = runtime.run_daily_repair(now=NOW, lease_owner="daily", timeout_seconds=6000)
    assert summary.ok
    with duckdb.connect(cursor_env.database) as connection:
        assert connection.execute(
            "SELECT next_start=range_end FROM ops.fitbit_repair_cursor "
            "WHERE cursor_id='manual:heart-rate'"
        ).fetchone()[0]


def test_daily_repair_includes_device_lookup_in_budget(cursor_env, monkeypatch):
    from personal_data_platform.sources.fitbit.acquisition import AcquisitionSummary

    warehouse = Warehouse(duckdb.connect(cursor_env.database))
    warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    warehouse.close()
    elapsed = [0]
    budgets = []
    runner = runtime._acquisition_runner()

    def devices():
        elapsed[0] = 20
        return runtime.SyncTime.from_datetime(NOW)

    monkeypatch.setattr(runtime.monotonic_time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(runner.client, "latest_tracker_sync", devices)
    monkeypatch.setattr(runtime, "_acquisition_runner", lambda: runner)

    def cursors(*args, timeout_seconds, **kwargs):
        budgets.append(timeout_seconds)
        return AcquisitionSummary()

    monkeypatch.setattr(runtime, "_run_cursors", cursors)
    assert runtime.run_daily_repair(now=NOW, lease_owner="daily", timeout_seconds=100).ok
    assert budgets == [80]


def test_daily_repair_makes_progress_with_unchanged_stale_device_sync(cursor_env, monkeypatch):
    warehouse = Warehouse(duckdb.connect(cursor_env.database))
    warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    warehouse.close()
    runner = runtime._acquisition_runner()
    monkeypatch.setattr(
        runner.client, "latest_tracker_sync", lambda: runtime.SyncTime.parse("2026-08-01T00:00:00Z")
    )
    monkeypatch.setattr(runtime, "_acquisition_runner", lambda: runner)
    monkeypatch.setenv("PDP_FITBIT_MAX_SCOPES", "2")
    first = runtime.run_daily_repair(now=NOW, lease_owner="daily", timeout_seconds=6000)
    first_windows = tuple(cursor_env.calls)
    cursor_env.calls.clear()
    second = runtime.run_daily_repair(now=NOW, lease_owner="daily", timeout_seconds=6000)
    assert not first.ok and not second.ok
    assert set(first_windows).isdisjoint(cursor_env.calls)
    assert min(window.start for window in cursor_env.calls) >= NOW - timedelta(days=8)

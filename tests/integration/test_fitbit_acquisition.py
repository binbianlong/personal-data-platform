from contextlib import closing
from datetime import UTC, datetime, timedelta

import duckdb

from personal_data_platform.sources.fitbit.models import (
    CapturedSnapshot,
    Notification,
    Record,
    Snapshot,
    Window,
)
from personal_data_platform.sources.fitbit.notifications import Delivery
from personal_data_platform.storage.motherduck import Warehouse

NOW = datetime(2026, 10, 5, 15, tzinfo=UTC)
WINDOW = Window(
    "steps", datetime(2026, 10, 1, 15, tzinfo=UTC), datetime(2026, 10, 2, 15, tzinfo=UTC)
)
STATE_TABLES = (
    "fitbit_scope",
    "fitbit_notification",
    "fitbit_notification_scope",
    "fitbit_attempt",
    "fitbit_scope_success",
    "fitbit_bundle",
    "fitbit_bundle_attempt",
    "fitbit_bundle_chunk",
    "fitbit_repair_cursor",
    "fitbit_device_sync",
)


class Broker:
    def __init__(self, batches):
        self.batches = list(batches)
        self.extended = []
        self.acked = []
        self.fail_ack = False

    def pull(self, **kwargs):
        return self.batches.pop(0) if self.batches else ()

    def extend(self, ids, *, seconds):
        self.extended.append((ids, seconds))

    def ack(self, ids):
        if self.fail_ack:
            raise TimeoutError("ack result unknown")
        self.acked.extend(ids)


def delivery(identity="n", windows=(WINDOW,), received_at=NOW):
    return Delivery(
        Notification(identity, "self", windows, received_at),
        "ack-" + identity,
        "message-" + identity,
    )


def setup(tmp_path, *, states=("A", "A", "B", "A"), fail_kind=None):
    from personal_data_platform.sources.fitbit.acquisition import AcquisitionRunner
    from personal_data_platform.sources.fitbit.adapter import FitbitSource

    path = str(tmp_path / "acquisition.duckdb")
    warehouse = Warehouse(duckdb.connect(path))
    warehouse.migrate(profile="west")
    for table in STATE_TABLES:
        warehouse.connection.execute(f"DROP TABLE IF EXISTS ops.{table}")
    warehouse.close()
    connections = []

    def factory():
        connections.append(1)
        return Warehouse(duckdb.connect(path))

    class Store:
        def __init__(self):
            self.objects = {}
            self.puts = self.gets = self.lists = 0
            self.fail_once = False

        def put_raw_object(self, key, payload):
            self.puts += 1
            raw = FitbitSource(version=3).parse_raw_key(
                key, storage_created_at=NOW, storage_generation=self.puts
            )
            self.objects[key] = (raw, payload)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("saved but response lost")
            return raw

        def get_raw(self, key, *, generation):
            self.gets += 1
            raw, payload = self.objects[key]
            assert raw.storage_generation == generation
            return payload

        def head_raw(self, key):
            return self.objects.get(key, (None,))[0]

        def list_raw(self, prefix):
            self.lists += 1
            return [row[0] for key, row in self.objects.items() if key.startswith(prefix)]

    class API:
        def __init__(self):
            self.calls = []

        def fetch_captured(self, window, *, subject_key):
            self.calls.append(window)
            if window.data_type == fail_kind:
                raise RuntimeError("API unavailable")
            state = states[min(len(self.calls) - 1, len(states) - 1)]
            records = (
                ()
                if state == "empty"
                else (
                    Record(
                        window.data_type,
                        "stable",
                        window.start,
                        window.start,
                        window.start + timedelta(minutes=1),
                        20 if state == "B" else 10,
                    ),
                )
            )
            return CapturedSnapshot(
                Snapshot(subject_key, window, NOW + timedelta(minutes=len(self.calls)), records),
                ({"unknown": state},),
            )

    store, api = Store(), API()
    runner = AcquisitionRunner(
        repository=store,
        client=api,
        warehouse_factory=factory,
        subject_key="self",
        clock=lambda: NOW + timedelta(hours=1),
    )
    return runner, store, api, factory, connections


def test_empty_pull_opens_no_warehouse(tmp_path):
    runner, _, _, _, connections = setup(tmp_path)
    assert runner.ingest(Broker([]), collect_seconds=0).ok
    assert connections == []


def test_initial_empty_pull_retries_within_collection_budget(tmp_path):
    runner, _, api, _, connections = setup(tmp_path)
    broker = Broker([(), (delivery(),)])
    result = runner.ingest(broker, collect_seconds=2)
    assert result.ok and result.acked_notifications == 1
    assert len(api.calls) == 1 and len(connections) == 1
    assert broker.acked == ["ack-n"]


def test_initial_poll_respects_execution_deadline(tmp_path):
    runner, _, _, _, connections = setup(tmp_path)
    elapsed, timeouts = [0.0], []
    runner.monotonic = lambda: elapsed[0]

    class SlowEmptyBroker(Broker):
        def pull(self, *, timeout_seconds, **kwargs):
            timeouts.append(timeout_seconds)
            elapsed[0] += timeout_seconds
            return ()

    assert runner.ingest(SlowEmptyBroker([]), collect_seconds=120, timeout_seconds=1).ok
    assert timeouts == [1] and connections == []


def test_fresh_fetch_without_attempt_tables_skips_unchanged_raw(tmp_path):
    runner, store, api, factory, _ = setup(tmp_path)
    for identity in ("old", "late"):
        broker = Broker([(delivery(identity),)])
        assert runner.ingest(broker, collect_seconds=0).ok
        assert broker.acked == ["ack-" + identity]
    assert len(api.calls) == 2
    assert store.puts == 1 and store.gets == 0
    with closing(factory()) as warehouse:
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM ops.ingestion_metadata WHERE status='succeeded'"
            )
            == 1
        )
        assert warehouse.query_value("SELECT source_sha256 IS NOT NULL FROM ops.fitbit_coverage")


def test_a_b_a_and_empty_complete_keep_changed_raw_and_delete(tmp_path):
    runner, store, _, factory, _ = setup(tmp_path, states=("A", "B", "A", "empty"))
    for i in range(4):
        assert runner.ingest(Broker([(delivery(str(i)),)]), collect_seconds=0).ok
    assert store.puts == 4
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 0
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_deleted_record") == 1


def test_scope_failure_does_not_block_other_days(tmp_path):
    runner, store, _, _, _ = setup(tmp_path, fail_kind="active-zone-minutes")
    failed = Window("active-zone-minutes", WINDOW.start, WINDOW.end)
    broker = Broker([(delivery("bad", (failed,)), delivery("good"))])
    summary = runner.ingest(broker, collect_seconds=0)
    assert summary.completed_scopes == 1 and summary.failed_scopes == 1
    assert broker.acked == ["ack-good"]
    assert store.puts == 1


def test_saved_raw_recovers_without_attempt_tables(tmp_path):
    runner, store, api, factory, _ = setup(tmp_path)
    store.fail_once = True
    first = Broker([(delivery(),)])
    assert not runner.ingest(first, collect_seconds=0).ok
    assert first.acked == [] and store.puts == 1
    retry = Broker([(delivery(),)])
    assert runner.ingest(retry, collect_seconds=0).ok
    assert retry.acked == ["ack-n"] and store.puts == 1 and len(api.calls) == 2
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_steps") == 10


def test_pending_raw_is_loaded_before_unchanged_fetch(tmp_path):
    runner, store, _, factory, _ = setup(tmp_path, states=("A", "B", "A"))
    assert runner.ingest(Broker([(delivery("a"),)]), collect_seconds=0).ok
    store.fail_once = True
    assert not runner.ingest(Broker([(delivery("b"),)]), collect_seconds=0).ok
    assert runner.ingest(Broker([(delivery("again"),)]), collect_seconds=0).ok
    assert store.puts == 3
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_steps") == 10
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM ops.ingestion_metadata WHERE status='succeeded'"
            )
            == 3
        )


def test_unchanged_fetch_prevents_stale_raw_replay(tmp_path):
    from personal_data_platform.sources.fitbit.writer import FitbitBatch

    runner, store, api, factory, _ = setup(tmp_path, states=("A", "A"))
    assert runner.ingest(Broker([(delivery("1"),)]), collect_seconds=0).ok
    stale_at = NOW + timedelta(seconds=90)
    assert runner.ingest(Broker([(delivery("2"),)]), collect_seconds=0).ok
    stale = Snapshot(
        "self",
        WINDOW,
        stale_at,
        (
            Record(
                "steps",
                "stable",
                WINDOW.start,
                WINDOW.start,
                WINDOW.start + timedelta(minutes=1),
                999,
            ),
        ),
    )
    with closing(factory()) as warehouse:
        FitbitBatch(stale).write_snapshot(warehouse.connection, source_key="stale", loaded_at=NOW)
        assert warehouse.query_value("SELECT value FROM base.fitbit_steps") == 10
    assert store.puts == 1 and len(api.calls) == 2


def test_redelivery_after_unknown_commit_is_idempotent(tmp_path):
    runner, store, api, factory, _ = setup(tmp_path)
    broker = Broker([(delivery(),)])
    broker.fail_ack = True
    runner.ingest(broker, collect_seconds=0)
    retry = Broker([(delivery(),)])
    assert runner.ingest(retry, collect_seconds=0).ok
    assert retry.acked == ["ack-n"] and store.puts == 1 and len(api.calls) == 2
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 1


def test_slow_scope_budget_commits_prefix_before_redelivery(tmp_path):
    runner, store, api, _, _ = setup(tmp_path)
    elapsed = [0.0]
    runner.monotonic = lambda: elapsed[0]
    original = api.fetch_captured

    def slow(*args, **kwargs):
        value = original(*args, **kwargs)
        elapsed[0] += 60
        return value

    api.fetch_captured = slow
    next_day = Window("steps", WINDOW.start + timedelta(days=1), WINDOW.end + timedelta(days=1))
    broker = Broker([(delivery("1"), delivery("2", (next_day,)))])
    result = runner.ingest(broker, collect_seconds=0, timeout_seconds=100)
    assert result.completed_scopes == 1 and result.deferred_scopes == 1
    assert broker.acked == ["ack-1"] and store.puts == 1


def test_lease_competitor_is_not_released(tmp_path):
    runner, _, _, factory, _ = setup(tmp_path)
    with closing(factory()) as warehouse:
        warehouse.acquire_job_lock("loader", "competitor", lease_seconds=7500)
    broker = Broker([(delivery(),)])
    assert runner.ingest(broker, collect_seconds=0).deferred_scopes == 1
    assert not broker.acked
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "competitor"


def test_oversized_scope_does_not_block_small_scopes(tmp_path, monkeypatch):
    import random

    from personal_data_platform.sources.fitbit import raw

    runner, store, api, _, _ = setup(tmp_path)
    monkeypatch.setattr(raw, "MAX_COMPRESSED_BYTES", 1500)
    original = api.fetch_captured

    def fetch(window, **kwargs):
        value = original(window, **kwargs)
        if window.data_type == "active-zone-minutes":
            return CapturedSnapshot(
                value.snapshot, ({"unknown": random.Random(1).randbytes(4000).hex()},)
            )
        return value

    api.fetch_captured = fetch
    huge = Window("active-zone-minutes", WINDOW.start, WINDOW.end)
    broker = Broker([(delivery("small"), delivery("huge", (huge,)))])
    result = runner.ingest(broker, collect_seconds=0)
    assert result.completed_scopes == 1 and result.failed_scopes == 1
    assert broker.acked == ["ack-small"] and store.puts == 1


def test_unchanged_minute_statistics_skip_raw_after_codec_round_trip(tmp_path):
    from personal_data_platform.sources.fitbit.models import (
        HeartRateMinute,
        HeartRateMinuteSnapshot,
    )

    runner, store, api, factory, _ = setup(tmp_path)
    window = Window("heart-rate", WINDOW.start, WINDOW.end)

    def fetch(window, *, subject_key):
        api.calls.append(window)
        return HeartRateMinuteSnapshot(
            subject_key,
            window,
            NOW + timedelta(minutes=len(api.calls)),
            (HeartRateMinute(window.start, window.start + timedelta(minutes=1), 90, 60, 100),),
            (),
        )

    api.fetch_heart_rate_minutes = fetch
    for identity in ("1", "2"):
        assert runner.ingest(Broker([(delivery(identity, (window,)),)]), collect_seconds=0).ok
    assert store.puts == 1
    with closing(factory()) as warehouse:
        assert warehouse.query_rows(
            "SELECT average,minimum,maximum FROM base.fitbit_heart_rate_minute"
        ) == [(90, 60, 100)]

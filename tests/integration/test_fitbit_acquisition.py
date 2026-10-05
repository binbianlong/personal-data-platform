from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit.models import (
    CapturedSnapshot,
    Notification,
    Snapshot,
    Window,
)
from personal_data_platform.sources.fitbit.notifications import Delivery
from personal_data_platform.storage.motherduck import Warehouse

NOW = datetime(2026, 10, 1, 15, tzinfo=UTC)
WINDOW = Window("steps", NOW, NOW + timedelta(days=1))


class Broker:
    def __init__(self, batches):
        self.batches = list(batches)
        self.extended = []
        self.acked = []

    def pull(self, **kwargs):
        return self.batches.pop(0) if self.batches else ()

    def extend(self, ids, *, seconds):
        self.extended.append((ids, seconds))

    def ack(self, ids):
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
    warehouse.migrate()
    warehouse.close()
    connections = []

    def factory():
        connections.append(1)
        return Warehouse(duckdb.connect(path))

    class Store:
        def __init__(self):
            self.objects = {}
            self.puts = 0
            self.gets = 0
            self.fail_once = False

        def put_raw_object(self, key, payload):
            self.puts += 1
            raw = FitbitSource(version=2).parse_raw_key(
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

        def list_raw(self, *args):
            raise AssertionError("acquisition must not LIST")

    class API:
        def __init__(self):
            self.calls = []

        def fetch_captured(self, window, *, subject_key):
            self.calls.append(window)
            if window.data_type == fail_kind:
                raise RuntimeError("API unavailable")
            state = states[min(len(self.calls) - 1, len(states) - 1)]
            return CapturedSnapshot(
                Snapshot(subject_key, window, NOW + timedelta(minutes=len(self.calls)), ()),
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


def test_old_success_and_late_notification_need_new_attempt(tmp_path):
    runner, store, api, _, _ = setup(tmp_path)
    for identity in ("old", "new"):
        broker = Broker([(delivery(identity),)])
        result = runner.ingest(broker, collect_seconds=0)
        assert result.ok and broker.acked == ["ack-" + identity]
    assert len(api.calls) == 2
    assert store.puts == 1 and store.gets == 0


def test_ack_waits_for_every_scope(tmp_path):
    runner, _, _, _, _ = setup(tmp_path, fail_kind="sleep")
    broker = Broker(
        [
            (
                delivery(
                    windows=(
                        WINDOW,
                        Window(
                            "sleep", NOW.replace(hour=0), NOW.replace(hour=0) + timedelta(days=1)
                        ),
                    )
                ),
            )
        ]
    )
    result = runner.ingest(broker, collect_seconds=0)
    assert result.failed_scopes == 1
    assert broker.acked == [] and not result.ok


def test_collect_extends_all_pending_deliveries(tmp_path):
    runner, _, api, _, _ = setup(tmp_path)
    clock = [0]

    def monotonic():
        clock[0] += 1
        return clock[0]

    runner.monotonic = monotonic
    broker = Broker([(delivery("one"),), (delivery("two"),)])
    result = runner.ingest(broker, collect_seconds=4)
    assert result.ok and set(broker.acked) == {"ack-one", "ack-two"}
    assert len(api.calls) == 1
    assert any(
        set(ids) == {"ack-one", "ack-two"} and 0 < seconds <= 600
        for ids, seconds in broker.extended
    )


def test_saved_raw_recovers_without_api_refetch(tmp_path):
    runner, store, api, _, _ = setup(tmp_path)
    store.fail_once = True
    first = Broker([(delivery(),)])
    assert not runner.ingest(first, collect_seconds=0).ok
    assert first.acked == []
    second = Broker([(delivery(),)])
    assert runner.ingest(second, collect_seconds=0).ok
    assert second.acked == ["ack-n"]
    assert len(api.calls) == 1 and store.puts == 1


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_unknown_bundle_commit_is_checked_after_reconnect(tmp_path, boundary):
    runner, store, api, factory, connections = setup(tmp_path)
    inject = [True]

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if sql.strip().upper() == "COMMIT" and inject[0]:
                inject[0] = False
                if boundary == "after_commit":
                    self.connection.execute(sql, *args)
                raise OSError("commit response unavailable")
            return self.connection.execute(sql, *args)

    def interrupted_factory():
        warehouse = factory()
        original = warehouse.load_objects

        def load(objects):
            if inject[0]:
                warehouse.connection = Connection(warehouse.connection)
            return original(objects)

        warehouse.load_objects = load
        return warehouse

    runner.warehouse_factory = interrupted_factory
    first = Broker([(delivery(),)])
    result = runner.ingest(first, collect_seconds=0)
    assert len(connections) == 2
    assert first.acked == (["ack-n"] if boundary == "after_commit" else [])
    assert result.ok == (boundary == "after_commit")
    if boundary == "before_commit":
        second = Broker([(delivery(),)])
        assert runner.ingest(second, collect_seconds=0).ok
        assert second.acked == ["ack-n"]
    assert store.puts == len(api.calls) == 1


def test_ack_failure_redelivery_uses_committed_attempt(tmp_path):
    runner, store, api, _, _ = setup(tmp_path)

    class FailedAck(Broker):
        def ack(self, ids):
            raise OSError("ack response unavailable")

    assert not runner.ingest(FailedAck([(delivery(),)]), collect_seconds=0).ok
    redelivery = Broker([(delivery(),)])
    assert runner.ingest(redelivery, collect_seconds=0).ok
    assert redelivery.acked == ["ack-n"]
    assert store.puts == len(api.calls) == 1


def test_slow_scope_budget_commits_completed_work_before_redelivery(tmp_path):
    runner, store, api, _, _ = setup(tmp_path)
    elapsed = [0]
    original = api.fetch_captured

    def slow(window, *, subject_key):
        elapsed[0] += 6
        return original(window, subject_key=subject_key)

    runner.monotonic = lambda: elapsed[0]
    api.fetch_captured = slow
    notification = delivery(windows=(Window("steps", NOW, NOW + timedelta(days=2)),))
    first = Broker([(notification,)])
    result = runner.ingest(first, collect_seconds=0, timeout_seconds=10)
    assert result.completed_scopes == result.deferred_scopes == 1
    assert store.puts == len(api.calls) == 1
    assert first.acked == []
    second = Broker([(notification,)])
    assert runner.ingest(second, collect_seconds=0, timeout_seconds=10).ok
    assert second.acked == ["ack-n"]
    assert store.puts == len(api.calls) == 2


def test_a_b_a_saves_every_changed_result(tmp_path):
    runner, store, api, _, _ = setup(tmp_path, states=("A", "B", "A"))
    for i in range(3):
        assert runner.ingest(Broker([(delivery(str(i)),)]), collect_seconds=0).ok
    assert store.puts == len(api.calls) == 3


def test_moved_stable_record_returning_to_old_scope_is_applied(tmp_path):
    from personal_data_platform.sources.fitbit.models import Record

    runner, store, api, factory, _ = setup(tmp_path)
    sequence = [0]
    day = NOW.replace(hour=0)

    def fetch(window, *, subject_key):
        sequence[0] += 1
        return CapturedSnapshot(
            Snapshot(
                subject_key,
                window,
                NOW + timedelta(hours=sequence[0]),
                (
                    Record(
                        "sleep",
                        "stable",
                        window.start,
                        window.start,
                        window.start + timedelta(hours=1),
                        45.0,
                        source_date=window.start.date(),
                    ),
                ),
            ),
            (),
        )

    api.fetch_captured = fetch
    runner.clock = lambda: NOW + timedelta(hours=sequence[0] + 1)
    for index in (0, 1, 0):
        start = day + timedelta(days=index)
        broker = Broker(
            [(delivery(str(sequence[0]), (Window("sleep", start, start + timedelta(days=1)),)),)]
        )
        assert runner.ingest(broker, collect_seconds=0).ok
    warehouse = factory()
    try:
        assert warehouse.query_value("SELECT cursor_at FROM base.fitbit_sleep") == day
        assert store.puts == 3
    finally:
        warehouse.close()


def test_lease_competitor_is_not_released(tmp_path):
    runner, _, api, factory, _ = setup(tmp_path)
    warehouse = factory()
    assert warehouse.acquire_job_lock("loader", "competitor", lease_seconds=7500)
    warehouse.close()
    broker = Broker([(delivery(),)])
    assert not runner.ingest(broker, collect_seconds=0).ok
    assert broker.extended[-1] == (("ack-n",), 0)
    warehouse = factory()
    assert (
        warehouse.query_value("SELECT owner_id FROM ops.job_lock WHERE job_name='loader'")
        == "competitor"
    )
    warehouse.close()
    assert not api.calls

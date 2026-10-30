from contextlib import closing
from datetime import timedelta

import pytest

from tests.integration.fitbit.test_fitbit_acquisition import NOW, direct_setup
from tests.unit.fitbit.test_fitbit_batch import Subscriber, raw_message

SUBSCRIPTION = "projects/test/subscriptions/fitbit"


def test_network_profile_keeps_commit_ack_and_separates_api_queue_and_db(
    tmp_path, monkeypatch, capfd
):
    import json

    from personal_data_platform import network_diagnostics as net

    monkeypatch.setattr(net.LOGGER, "handlers", [])
    monkeypatch.setattr(net.LOGGER, "level", net.LOGGER.level)
    monkeypatch.setattr(net.LOGGER, "propagate", net.LOGGER.propagate)
    sent = [0]
    monkeypatch.setenv("PDP_NETWORK_DIAGNOSTICS", "true")
    monkeypatch.setattr(net, "read_counters", lambda: net.Counters(sent[0], 0, ("eth0",)))
    runner, api, _ = direct_setup(tmp_path, states=(None,))
    fetch = api.fetch_snapshot

    def measured_fetch(*args, **kwargs):
        sent[0] += 300
        return fetch(*args, **kwargs)

    api.fetch_snapshot = measured_fetch

    class MeasuredSubscriber(Subscriber):
        def pull(self, **kwargs):
            sent[0] += 50
            return super().pull(**kwargs)

        def acknowledge(self, **kwargs):
            sent[0] += 70
            return super().acknowledge(**kwargs)

    subscriber = MeasuredSubscriber([[raw_message("a")]])
    result = drainer(runner, subscriber).run()
    assert result.ok and result.acked == result.completed == 1
    entries = [json.loads(line) for line in capfd.readouterr().err.splitlines()]
    summary = next(e["summary"] for e in entries if e.get("event") == "network_profile")
    assert summary["correlation_id"] == result.batch_id
    assert summary["workload"] == "fitbit_batch"
    assert summary["sent_bytes"] == 470
    phases = {row["phase"]: row for row in summary["phases"]}
    assert phases["fitbit_api"]["sent_bytes"] == 300
    assert phases["pubsub_pull"]["sent_bytes"] == 100
    assert phases["pubsub_ack"]["sent_bytes"] == 70
    assert phases["lock_check"]["calls"] > 0
    assert phases["coverage_write"]["calls"] > 0
    assert "secret-ack" not in json.dumps(summary)


def drainer(runner, subscriber):
    from personal_data_platform.sources.fitbit.batch import NotificationDrainer

    return NotificationDrainer(runner=runner, subscriber=subscriber, subscription=SUBSCRIPTION)


def test_drain_groups_duplicates_with_one_session_and_commits_before_ack(tmp_path, caplog):
    runner, api, factory = direct_setup(tmp_path, states=(None,))
    messages = [raw_message("a"), raw_message("b")]
    from tests.unit.fitbit.test_fitbit_batch import NOW as RECEIVED_AT

    runner.clock = lambda: RECEIVED_AT + timedelta(minutes=1)

    class ObservingSubscriber(Subscriber):
        def acknowledge(self, **kwargs):
            with closing(factory()) as warehouse:
                assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 1
            super().acknowledge(**kwargs)

    subscriber = ObservingSubscriber([messages])
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    result = drainer(runner, subscriber).run()
    assert result.ok and result.received == result.acked == 2
    assert result.grouped == result.completed == result.session_opens == len(api.calls) == 1
    assert set(subscriber.acks) == {m.ack_id for m in messages}
    entries = [
        r for r in caplog.records if getattr(r, "event", None) == "fitbit_notification_processed"
    ]
    assert len(entries) == 2
    scopes = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert {r.summary["group_id"] for r in entries} == {scopes[0].summary["notification_id"]}
    assert all(r.summary["lag_seconds"] >= 0 and r.summary["committed_at"] for r in entries)
    assert "secret-ack" not in repr([r.__dict__ for r in caplog.records])


def test_empty_and_paused_drain_never_open_session_or_ack(tmp_path):
    runner, api, _ = direct_setup(tmp_path)
    runner.warehouse_factory = lambda: pytest.fail("must not open warehouse")
    assert drainer(runner, Subscriber()).run().ok
    runner.paused = True
    subscriber = Subscriber([[raw_message("a")]])
    result = drainer(runner, subscriber).run()
    assert not result.ok and not api.calls and not subscriber.acks


def test_failed_ack_never_counts_as_acknowledged(tmp_path):
    runner, _, _ = direct_setup(tmp_path, states=(None,))

    class FailingAck(Subscriber):
        def acknowledge(self, **kwargs):
            raise TimeoutError("acknowledgment outcome unknown")

    result = drainer(runner, FailingAck([[raw_message("a")]])).run()
    assert not result.ok and result.acked == 0 and result.completed == 1


def test_independent_failed_unit_does_not_block_successful_unit(tmp_path):
    runner, api, _ = direct_setup(tmp_path, states=(None,))
    first = raw_message("a")
    second = raw_message("b")
    import json

    payload = json.loads(second.message.data)
    payload["windows"][0]["start"] = (NOW - timedelta(days=3)).isoformat()
    payload["windows"][0]["end"] = (NOW - timedelta(days=3) + timedelta(minutes=1)).isoformat()
    second.message.data = json.dumps(payload).encode()
    fetch = api.fetch_snapshot
    attempts = []

    def failing(window, **kwargs):
        attempts.append(window)
        if len(attempts) == 1:
            raise TimeoutError("API failure")
        return fetch(window, **kwargs)

    api.fetch_snapshot = failing
    subscriber = Subscriber([[first, second]])
    result = drainer(runner, subscriber).run()
    assert not result.ok and result.failed == 1 and result.completed == 1
    assert subscriber.acks == [second.ack_id]


def retime(message, start, end, *, mode=None):
    import json

    payload = json.loads(message.message.data)
    payload["windows"][0]["start"] = start.isoformat()
    payload["windows"][0]["end"] = end.isoformat()
    if mode:
        payload["schema_version"] = 1
        payload["acquisition_mode"] = mode
    message.message.data = json.dumps(payload).encode()
    return message


def test_two_200_second_units_fit_shared_budget_and_keep_one_session(tmp_path):
    runner, api, _ = direct_setup(tmp_path, states=(None,))
    elapsed = [0.0]
    runner.monotonic = lambda: elapsed[0]
    fetch = api.fetch_snapshot

    def slow(*args, **kwargs):
        result = fetch(*args, **kwargs)
        elapsed[0] += 200
        return result

    api.fetch_snapshot = slow
    messages = [
        raw_message("a"),
        retime(
            raw_message("b"),
            NOW - timedelta(days=2),
            NOW - timedelta(days=2) + timedelta(minutes=1),
        ),
    ]
    subscriber = Subscriber([messages])
    result = drainer(runner, subscriber).run()
    assert result.ok and result.acked == result.completed == 2
    assert result.elapsed_seconds == 400 and result.session_opens == 1


def test_legacy_message_waits_for_every_normalized_day_before_ack(tmp_path):
    runner, api, _ = direct_setup(tmp_path, states=(None,))
    # Cross Tokyo midnight while remaining below the queue's one-day upper bound.
    notice = retime(
        raw_message("legacy"), NOW - timedelta(minutes=1), NOW + timedelta(hours=4), mode="legacy"
    )
    fetch = api.fetch_snapshot
    attempts = []

    def fail_second(*args, **kwargs):
        attempts.append(args[0])
        if len(attempts) == 2:
            raise TimeoutError("second day failed")
        return fetch(*args, **kwargs)

    api.fetch_snapshot = fail_second
    subscriber = Subscriber([[notice]])
    result = drainer(runner, subscriber).run()
    assert len(attempts) == result.grouped == 2
    assert result.completed == 1 and not result.ok and not subscriber.acks
    api.fetch_snapshot = fetch
    retry = Subscriber([[notice]])
    assert drainer(runner, retry).run().ok and retry.acks == [notice.ack_id]


def test_unknown_commit_stops_all_later_units_without_ack(tmp_path):
    runner, api, factory = direct_setup(tmp_path, states=(None,))

    def uncertain_factory():
        warehouse = factory()
        warehouse.migrate()
        connection = warehouse.connection

        class UncertainConnection:
            commits = 0

            def execute(self, sql, *args, **kwargs):
                result = connection.execute(sql, *args, **kwargs)
                if sql == "COMMIT":
                    self.commits += 1
                    if self.commits == 2:
                        raise RuntimeError("commit response unknown")
                return result

            def __getattr__(self, name):
                return getattr(connection, name)

        warehouse.connection = UncertainConnection()
        return warehouse

    runner.warehouse_factory = uncertain_factory
    messages = [
        raw_message("a"),
        retime(
            raw_message("b"),
            NOW - timedelta(days=2),
            NOW - timedelta(days=2) + timedelta(minutes=1),
        ),
    ]
    subscriber = Subscriber([messages])
    result = drainer(runner, subscriber).run()
    assert not result.ok and len(api.calls) == 1 and not subscriber.acks


def test_expensive_factory_uses_global_budget_before_migration_or_api(tmp_path):
    runner, api, factory = direct_setup(tmp_path, states=(None,))
    elapsed = [0]
    runner.monotonic = lambda: elapsed[0]

    def slow_factory():
        warehouse = factory()
        elapsed[0] = 470
        warehouse.migrate = lambda: pytest.fail("factory exhausted budget")
        return warehouse

    runner.warehouse_factory = slow_factory
    subscriber = Subscriber([[raw_message("a")]])
    result = drainer(runner, subscriber).run()
    assert not result.ok and result.deferred == 1
    assert not api.calls and not subscriber.acks


def test_drain_endpoint_returns_safe_json_without_reading_request_body(tmp_path):
    from dataclasses import asdict

    from fastapi.testclient import TestClient

    from personal_data_platform.sources.fitbit.service import create_worker_app

    runner, _, _ = direct_setup(tmp_path)
    drain = drainer(runner, Subscriber())
    client = TestClient(create_worker_app(drainer=drain))
    response = client.post("/notifications/fitbit/drain", content="private ignored body")
    assert response.status_code == 200
    assert set(response.json()) == set(asdict(drain.run()))
    runner.paused = True
    assert client.post("/notifications/fitbit/drain").status_code == 503


def test_warehouse_close_failure_still_records_batch_summary(tmp_path, caplog):
    runner, _, factory = direct_setup(tmp_path, states=(None,))

    def failing_close_factory():
        warehouse = factory()
        close = warehouse.close

        def fail_close():
            close()
            raise RuntimeError("private cleanup text")

        warehouse.close = fail_close
        return warehouse

    runner.warehouse_factory = failing_close_factory
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    result = drainer(runner, Subscriber([[raw_message("a")]])).run()
    assert not result.ok and result.acked == 1
    assert len([r for r in caplog.records if getattr(r, "event", None) == "fitbit_batch"]) == 1
    assert "private cleanup text" not in caplog.text


def test_one_hundred_independent_units_share_lifecycle_and_preserve_each_commit(tmp_path):
    import collections

    runner, api, factory = direct_setup(tmp_path, states=(None,))
    with closing(factory()) as warehouse:
        warehouse.migrate()
    calls = collections.Counter()

    def counted_factory():
        calls["connect"] += 1
        warehouse = factory()
        for name in ("migrate", "acquire_job_lock", "release_job_lock", "close"):
            method = getattr(warehouse, name)

            def counted(*args, _method=method, _name=name, **kwargs):
                calls[_name] += 1
                return _method(*args, **kwargs)

            setattr(warehouse, name, counted)
        return warehouse

    runner.warehouse_factory = counted_factory
    notices = [
        retime(
            raw_message(str(i)),
            NOW - timedelta(days=i + 1),
            NOW - timedelta(days=i + 1) + timedelta(minutes=1),
        )
        for i in range(100)
    ]
    subscriber = Subscriber([notices])
    result = drainer(runner, subscriber).run()
    assert result.ok and result.completed == result.acked == len(api.calls) == 100
    assert calls == {
        "connect": 1,
        "migrate": 1,
        "acquire_job_lock": 1,
        "release_job_lock": 1,
        "close": 1,
    }


def test_legacy_subday_notifications_fetch_the_same_day_once(tmp_path):
    runner, api, _ = direct_setup(tmp_path, states=(None,))
    start = NOW - timedelta(hours=1)
    first = retime(raw_message("a"), start, start + timedelta(minutes=1), mode="legacy")
    second = retime(
        raw_message("b"), start + timedelta(minutes=2), start + timedelta(minutes=3), mode="legacy"
    )
    subscriber = Subscriber([[first, second]])
    result = drainer(runner, subscriber).run()
    assert result.ok and result.acked == 2
    assert len(api.calls) == result.grouped == 1
    assert set(subscriber.acks) == {first.ack_id, second.ack_id}

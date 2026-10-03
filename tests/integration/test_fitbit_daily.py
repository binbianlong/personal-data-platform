from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit.adapter import FitbitSource
from personal_data_platform.sources.fitbit.api import SyncTime
from personal_data_platform.sources.fitbit.daily import collect_daily, collect_range
from personal_data_platform.sources.fitbit.models import DATA_TYPES, Record, Snapshot, Window
from personal_data_platform.sources.fitbit.raw import encode_snapshot
from personal_data_platform.sources.fitbit.state import DailyStateStore
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConnectionError
from tests.sql_helpers import TracedConnection

NOW = datetime(2026, 10, 3, 4, tzinfo=UTC)


class Repository:
    def __init__(self):
        self.objects = {}
        self.puts = self.heads = self.reads = 0
        self.interruption = None

    def list_raw(self, prefix):
        raise AssertionError("daily collection must not list GCS")

    def put_raw_object(self, key, content):
        self.puts += 1
        if self.interruption == "before":
            self.interruption = None
            raise OSError("upload interrupted")
        raw = FitbitSource().parse_raw_key(
            key, storage_created_at=NOW, storage_generation=self.puts
        )
        self.objects[key] = (raw, content)
        if self.interruption == "after":
            self.interruption = None
            raise OSError("upload acknowledgement lost")
        return raw

    def head_raw(self, key):
        self.heads += 1
        return self.objects[key][0] if key in self.objects else None

    def get_raw(self, key, *, generation):
        self.reads += 1
        raw, content = self.objects[key]
        assert generation == raw.storage_generation
        return content


class API:
    def __init__(self):
        self.calls = []
        self.sync_at = NOW
        self.version = "A"
        self.value = 12.0
        self.failed_type = None

    def latest_tracker_sync(self):
        return SyncTime.from_datetime(self.sync_at)

    def fetch(self, window, *, subject_key):
        self.calls.append(window)
        if window.data_type == self.failed_type:
            raise OSError("API interrupted")
        rows = ()
        if window.data_type == "steps" and self.value is not None:
            rows = (
                Record(
                    "steps",
                    str(window.start),
                    window.start,
                    window.start,
                    window.start + timedelta(minutes=1),
                    self.value,
                ),
            )
        return Snapshot(
            subject_key,
            window,
            NOW + timedelta(seconds=len(self.calls)),
            rows,
            source_payload=({"unknown": self.version},),
        )


@pytest.fixture
def setup(tmp_path):
    warehouse = Warehouse(duckdb.connect(str(tmp_path / "daily.duckdb")))
    warehouse.migrate()
    repository, api = Repository(), API()
    yield warehouse, repository, api
    warehouse.close()


def run(setup, **kwargs):
    warehouse, repository, api = setup
    return collect_daily(repository, warehouse, client=api, subject_key="self", now=NOW, **kwargs)


def test_slow_daily_defers_before_its_shared_lease_can_expire(setup, monkeypatch):
    from personal_data_platform.sources.fitbit import daily

    warehouse, repository, api = setup
    elapsed = [0.0]
    original_fetch = api.fetch

    def slow_fetch(*args, **kwargs):
        result = original_fetch(*args, **kwargs)
        elapsed[0] += 10 * 60
        return result

    monkeypatch.setattr(daily, "monotonic", lambda: elapsed[0], raising=False)
    monkeypatch.setattr(api, "fetch", slow_fetch)
    result = run(setup)
    assert result.status == "deferred" and result.fetched_windows == 9
    assert repository.puts == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_daily_state") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    monkeypatch.setattr(api, "fetch", original_fetch)
    assert run(setup).status == "succeeded"


@pytest.mark.parametrize("tracker_advances", [False, True])
def test_delayed_historical_points_are_rechecked_after_backfill(setup, tracker_advances):
    warehouse, repository, api = setup
    first = NOW - timedelta(days=30)
    api.sync_at, api.value = first, None
    assert (
        collect_daily(repository, warehouse, client=api, subject_key="self", now=first).status
        == "succeeded"
    )
    api.sync_at = NOW
    assert run(setup).status == "succeeded"
    before = len(api.calls)
    api.value = 12.0
    if tracker_advances:
        api.sync_at = NOW + timedelta(days=1)
    assert (
        collect_daily(
            repository, warehouse, client=api, subject_key="self", now=NOW + timedelta(days=1)
        ).status
        == "succeeded"
    )
    assert any(window.start < NOW - timedelta(days=20) for window in api.calls[before:])
    assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") > 7 * 12


def test_historical_recheck_resumes_under_the_combined_ninety_day_budget(setup):
    warehouse, repository, api = setup
    first = NOW - timedelta(days=120)
    api.sync_at, api.value = first, None
    assert (
        collect_daily(repository, warehouse, client=api, subject_key="self", now=first).status
        == "succeeded"
    )
    api.sync_at = NOW
    assert run(setup).status == "deferred"
    assert run(setup).status == "succeeded"
    api.value = 12.0
    tomorrow = NOW + timedelta(days=1)

    def check():
        before = len(api.calls)
        result = collect_daily(repository, warehouse, client=api, subject_key="self", now=tomorrow)
        assert len(api.calls) - before <= 90 * len(DATA_TYPES)
        return result

    assert check().status == "deferred"
    assert check().status == "succeeded"
    assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") > 100 * 12


def test_unchanged_historical_rechecks_skip_raw_and_expire_after_seven_days(setup):
    warehouse, repository, api = setup
    first = NOW - timedelta(days=30)
    api.sync_at, api.value = first, None
    collect_daily(repository, warehouse, client=api, subject_key="self", now=first)
    api.sync_at = NOW
    run(setup)
    active = DailyStateStore(warehouse, "self").read().recheck
    assert active is not None

    def check(days):
        before = len(api.calls)
        result = collect_daily(
            repository, warehouse, client=api, subject_key="self", now=NOW + timedelta(days=days)
        )
        return result, len(api.calls) - before

    summary, count = check(1)
    assert count > 35 and summary.raw_saved == 1
    assert repository.heads == repository.reads == 0
    check(7)
    summary, count = check(8)
    assert count == 35 and summary.status == "succeeded"
    assert DailyStateStore(warehouse, "self").read().recheck is None


def test_interrupted_historical_recheck_retains_its_scope_and_manual_sync_preserves_it(setup):
    warehouse, repository, api = setup
    first = NOW - timedelta(days=30)
    api.sync_at, api.value = first, None
    collect_daily(repository, warehouse, client=api, subject_key="self", now=first)
    api.sync_at = NOW
    run(setup)
    expected = DailyStateStore(warehouse, "self").read().recheck
    collect_range(
        repository,
        warehouse,
        client=api,
        subject_key="self",
        windows=(Window("steps", NOW - timedelta(days=1), NOW),),
    )
    assert DailyStateStore(warehouse, "self").read().recheck == expected
    api.value, repository.interruption = 12.0, "after"
    tomorrow = NOW + timedelta(days=1)
    with pytest.raises(OSError):
        collect_daily(repository, warehouse, client=api, subject_key="self", now=tomorrow)
    assert (
        collect_daily(repository, warehouse, client=api, subject_key="self", now=tomorrow).status
        == "succeeded"
    )
    assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") > 7 * 12
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_batch_intent") == 0


def test_daily_bundles_seven_completed_days_once_without_gcs_reads(setup):
    warehouse, repository, api = setup
    summary = run(setup)
    assert summary.status == "succeeded" and summary.raw_saved == 1
    assert summary.fetched_windows == 35
    assert repository.puts == 1 and repository.heads == repository.reads == 0
    assert {window.data_type for window in api.calls} == set(DATA_TYPES)
    assert all(window.end <= NOW for window in api.calls)
    assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") == 84
    assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 1
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_batch_intent") == 0
    assert warehouse.query_value("SELECT last_daily_run FROM ops.fitbit_daily_state") == date(
        2026, 10, 3
    )
    before = len(api.calls)
    assert run(setup).status == "succeeded"
    assert len(api.calls) == before and repository.puts == 1


def test_manual_recheck_skips_unchanged_and_preserves_unknown_changes_a_b_a_and_deletion(setup):
    warehouse, repository, api = setup
    window = Window("steps", NOW - timedelta(days=1), NOW)

    def sync():
        return collect_range(
            repository, warehouse, client=api, subject_key="self", windows=(window,)
        )

    assert sync().raw_saved == 1
    assert sync().raw_saved == 0
    api.version = "B"
    assert sync().raw_saved == 1
    api.version = "A"
    assert sync().raw_saved == 1
    api.value = None
    assert sync().raw_saved == 1
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 0
    assert repository.puts == 4 and repository.heads == repository.reads == 0


def test_partial_api_failure_does_not_save_or_advance_daily_progress(setup):
    warehouse, repository, api = setup
    api.failed_type = "heart-rate"
    with pytest.raises(OSError, match="API interrupted"):
        run(setup)
    assert repository.puts == 0
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_batch_intent") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_daily_state") == 0


@pytest.mark.parametrize("interruption", ["before", "after"])
def test_recovery_after_upload_interruption_uses_known_keys_without_listing(setup, interruption):
    warehouse, repository, api = setup
    repository.interruption = interruption
    with pytest.raises(OSError):
        run(setup)
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_batch_intent") == 1
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 0
    before = len(api.calls)
    assert run(setup).status == "succeeded"
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_batch_intent") == 0
    assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") == 84
    if interruption == "after":
        assert len(api.calls) == before and repository.puts == 1
        assert repository.heads == repository.reads == 1
    else:
        assert len(api.calls) == 2 * before and repository.puts == 2
        assert repository.heads == 1 and repository.reads == 0


def test_recovery_after_analysis_commit_does_not_read_raw_again(setup, monkeypatch, tmp_path):
    warehouse, repository, api = setup
    original = warehouse.load_object

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        warehouse.connection_usable = False
        raise WarehouseConnectionError("interrupted after load commit")

    monkeypatch.setattr(warehouse, "load_object", interrupted)
    with pytest.raises(WarehouseConnectionError, match="interrupted after load commit"):
        run(setup)
    warehouse.close()
    reopened = Warehouse(duckdb.connect(str(tmp_path / "daily.duckdb")))
    try:
        reopened.connection.execute("DELETE FROM ops.job_lock")
        assert run((reopened, repository, api)).status == "succeeded"
        assert len(api.calls) == 35 and repository.puts == 1
        assert repository.heads == repository.reads == 0
        assert reopened.query_value("SELECT count(*) FROM ops.fitbit_batch_intent") == 0
    finally:
        reopened.close()


def test_pause_and_shared_loader_contention_make_no_external_calls(setup):
    warehouse, repository, api = setup
    assert run(setup, paused=True).status == "paused"
    assert warehouse.acquire_job_lock("loader", "other", lease_seconds=3600)
    assert run(setup).status == "deferred"
    assert api.calls == [] and repository.puts == repository.heads == repository.reads == 0


def test_next_day_refetches_recent_days_and_saves_only_new_complete_day(setup):
    warehouse, repository, api = setup
    run(setup)
    api.sync_at = NOW + timedelta(days=1)
    summary = collect_daily(repository, warehouse, client=api, subject_key="self", now=api.sync_at)
    assert summary.raw_saved == 1 and summary.raw_skipped == 30
    assert repository.puts == 2 and repository.heads == repository.reads == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 40


def test_seven_day_recheck_batches_skip_queries_and_timestamp_updates(setup):
    warehouse, repository, api = setup
    run(setup)
    connection = TracedConnection(warehouse.connection)
    warehouse.connection = connection

    summary = collect_daily(
        repository, warehouse, client=api, subject_key="self", now=NOW + timedelta(days=1)
    )

    assert summary.status == "succeeded" and summary.raw_saved == 1 and summary.raw_skipped == 30
    assert repository.puts == 2 and repository.heads == repository.reads == 0
    assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") == 96
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM ops.fitbit_coverage WHERE fetched_at > ?",
            [NOW + timedelta(seconds=35)],
        )
        == 35
    )
    skip_queries = [
        sql
        for sql in connection.statements
        if sql.startswith("SELECT")
        and (
            sql.startswith("SELECT count(*) FROM ops.fitbit_raw_intent")
            or (
                "ops.fitbit_coverage" in sql
                and "source_sha256" in sql
                and "content_sha256" not in sql
            )
        )
    ]
    updates = [
        sql
        for sql in connection.statements
        if "UPDATE ops.fitbit_coverage" in sql and "SET fetched_at" in sql
    ]
    assert len(skip_queries) == 1
    assert len(updates) == 1


def test_tracker_sync_catches_up_data_older_than_lookback_in_bounded_bundles(setup):
    warehouse, repository, api = setup
    old = NOW - timedelta(days=30)
    api.sync_at = old
    collect_daily(repository, warehouse, client=api, subject_key="self", now=old)
    api.sync_at = NOW
    summary = run(setup)
    assert summary.status == "succeeded"
    assert len(api.calls) > 35 * 2
    assert summary.raw_saved >= 4
    assert all(
        len(FitbitSource().decode(raw, __import__("gzip").decompress(content)).snapshots) <= 35
        for raw, content in repository.objects.values()
    )
    assert warehouse.query_value("SELECT completed_through FROM ops.fitbit_daily_state") == date(
        2026, 10, 2
    )


def test_long_backfill_resumes_after_daily_limit_without_losing_progress(setup):
    warehouse, repository, api = setup
    old = NOW - timedelta(days=120)
    api.sync_at = old
    collect_daily(repository, warehouse, client=api, subject_key="self", now=old)
    api.sync_at = NOW
    assert run(setup).status == "deferred"
    assert run(setup).status == "succeeded"
    assert warehouse.query_value("SELECT completed_through FROM ops.fitbit_daily_state") == date(
        2026, 10, 2
    )


def test_late_api_data_is_refetched_when_tracker_timestamp_is_unchanged(setup):
    warehouse, repository, api = setup
    run(setup)
    api.value = 20
    summary = collect_daily(
        repository, warehouse, client=api, subject_key="self", now=NOW + timedelta(days=1)
    )
    assert summary.raw_saved == 1
    assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") == 7 * 20 + 12


def test_missing_legacy_raw_intent_is_refetched_and_retired(setup):
    warehouse, repository, api = setup
    window = Window("steps", NOW - timedelta(days=1), NOW)
    old = Snapshot("self", window, NOW - timedelta(seconds=1), ())
    key, _ = encode_snapshot(old)
    warehouse.connection.execute(
        "INSERT INTO ops.fitbit_raw_intent VALUES (?,?,?,?,?,?,?,?)",
        ["legacy", 0, "self", "steps", window.start, window.end, key, old.fetched_at],
    )
    assert run(setup).status == "succeeded"
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    assert repository.heads == 1 and repository.reads == 0


def test_manual_long_range_uses_seven_day_bundles(setup):
    import gzip

    warehouse, repository, api = setup
    window = Window("steps", NOW - timedelta(days=20), NOW)
    summary = collect_range(
        repository, warehouse, client=api, subject_key="self", windows=(window,)
    )
    assert summary.raw_saved == 3 and summary.fetched_windows == 20
    assert all(
        len(FitbitSource().decode(raw, gzip.decompress(content)).snapshots) <= 7
        for raw, content in repository.objects.values()
    )

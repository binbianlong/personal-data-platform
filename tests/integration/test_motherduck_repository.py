from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.screen_time.parser import PARSER_VERSION
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import (
    Warehouse,
    WarehouseConfig,
    connect,
)
from tests.screen_time_helpers import _raw, _record


def test_cloud_connection_is_scoped_to_the_requested_database(monkeypatch):
    import duckdb

    calls = []
    connection = object()

    def open_connection(database, *, config):
        calls.append((database, config))
        return connection

    monkeypatch.setattr(duckdb, "connect", open_connection)
    assert connect(WarehouseConfig("production", "synthetic-token")) is connection
    assert calls == [
        (
            "md:production",
            {"motherduck_token": "synthetic-token", "motherduck_attach_mode": "single"},
        )
    ]


def test_screen_time_receipts_prune_only_confirmed_compacted_scope(tmp_path):
    import gzip

    from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
    from tests.screen_time_helpers import Repository, event, segb

    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "receipts.duckdb"))))
    warehouse.migrate()
    repository = Repository()
    source = ScreenTimeSource()
    old = repository.add("100", segb(event("app.old"))[0])
    current = repository.add("100", segb(event("app.current"))[0])
    unrelated = repository.add("200", segb(event("app.other"))[0])
    for raw in (old, current, unrelated):
        warehouse.load_object(
            raw,
            byte_size=1,
            batch=source.decode(raw, gzip.decompress(repository.objects[raw.key][1])),
        )
    warehouse.prune_screen_time_receipts([current], parser_version=source.parser_version)
    assert warehouse.query_rows(
        "SELECT object_key FROM ops.ingestion_metadata ORDER BY object_key"
    ) == [(key,) for key in sorted((current.key, unrelated.key))]
    warehouse.close()


def test_screen_time_receipts_need_one_confirmed_live_raw(tmp_path):
    import gzip

    from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
    from tests.screen_time_helpers import Repository, event, segb

    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "receipts.duckdb"))))
    warehouse.migrate()
    repository = Repository()
    source = ScreenTimeSource()
    old = repository.add("100", segb(event("app.old"))[0])
    current = repository.add("100", segb(event("app.current"))[0])
    for raw in (old, current):
        warehouse.load_object(
            raw,
            byte_size=1,
            batch=source.decode(raw, gzip.decompress(repository.objects[raw.key][1])),
        )
    connection = warehouse.connection

    class NoTransaction:
        def execute(self, sql, *args):
            assert not sql.startswith(("BEGIN", "COMMIT"))
            return connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(connection, name)

    warehouse.connection = NoTransaction()
    warehouse.prune_screen_time_receipts([], parser_version=source.parser_version)
    warehouse.prune_screen_time_receipts([old, current], parser_version=source.parser_version)
    assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 2
    unconfirmed = replace(current, storage_generation=2)
    warehouse.prune_screen_time_receipts([unconfirmed], parser_version=source.parser_version)
    assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 2
    warehouse.close()


def test_run_history_prunes_after_seven_days_but_keeps_daily_gap_marker():
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    now = datetime(2026, 10, 11, tzinfo=UTC)
    old = now - timedelta(days=8)
    recent = now - timedelta(days=6)
    warehouse.connection.execute(
        "INSERT INTO ops.job_run VALUES "
        "('old-daily', 'daily-reconciliation-west', 'succeeded', ?, ?, ?), "
        "('old-other', 'loader', 'failed', ?, ?, NULL), "
        "('recent', 'loader', 'succeeded', ?, ?, NULL)",
        [old, old, '{"last_completed_target_date":"2026-10-02"}', old, old, recent, recent],
    )
    warehouse.prune_run_history(now=now)
    assert warehouse.query_rows("SELECT run_id FROM ops.job_run ORDER BY run_id") == [
        ("old-daily",),
        ("recent",),
    ]
    warehouse.close()


def test_run_history_keeps_latest_daily_marker_across_previous_and_current_names():
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    now = datetime(2026, 10, 11, tzinfo=UTC)
    older = now - timedelta(days=9)
    newer = now - timedelta(days=8)
    warehouse.connection.execute(
        "INSERT INTO ops.job_run VALUES "
        "('previous', 'daily-reconciliation-west', 'succeeded', ?, ?, ?), "
        "('current', 'daily-reconciliation', 'succeeded', ?, ?, ?)",
        [
            older,
            older,
            '{"last_completed_target_date":"2026-10-01"}',
            newer,
            newer,
            '{"last_completed_target_date":"2026-10-02"}',
        ],
    )
    warehouse.prune_run_history(now=now)
    assert warehouse.query_rows("SELECT run_id FROM ops.job_run") == [("current",)]
    warehouse.close()


def test_migration_and_object_load_are_idempotent(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "test.duckdb"))))
    try:
        warehouse.migrate()
        raw = _raw()

        assert warehouse.load_object(raw, byte_size=100, batch=ScreenTimeBatch([_record(raw)])) == 1
        assert warehouse.load_object(raw, byte_size=100, batch=ScreenTimeBatch([_record(raw)])) == 0
        assert warehouse.succeeded_keys(source_id="screen_time", stream="App.InFocus") == {raw.key}
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
        assert (
            warehouse.query_value("SELECT storage_created_at FROM ops.ingestion_metadata")
            == raw.storage_created_at
        )
    finally:
        warehouse.close()


def test_failed_load_can_be_retried(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "test.duckdb"))))
    try:
        warehouse.migrate()
        raw = _raw()
        warehouse.mark_failed(raw, byte_size=0, error=ValueError("broken"))

        assert warehouse.ingestion_counts(source_id="screen_time", stream="App.InFocus") == {
            "failed": 1
        }
        assert (
            warehouse.query_value("SELECT storage_created_at FROM ops.ingestion_metadata")
            == raw.storage_created_at
        )
        assert warehouse.load_object(raw, byte_size=100, batch=ScreenTimeBatch([_record(raw)])) == 1
        assert warehouse.ingestion_counts(source_id="screen_time", stream="App.InFocus") == {
            "succeeded": 1
        }
    finally:
        warehouse.close()


def test_expired_key_can_be_reloaded_with_a_new_storage_creation_time(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "test.duckdb"))))
    try:
        warehouse.migrate()
        original = _raw()
        assert (
            warehouse.load_object(
                original, byte_size=100, batch=ScreenTimeBatch([_record(original)])
            )
            == 1
        )
        warehouse.connection.execute(
            "UPDATE ops.ingestion_metadata SET retention_expired_at = current_timestamp "
            "WHERE object_key = ?",
            [original.key],
        )
        assert warehouse.succeeded_keys(source_id="screen_time", stream="App.InFocus") == set()

        recreated = _raw(
            storage_created_at=datetime(2026, 10, 27, 1, tzinfo=UTC),
            storage_generation=2,
        )
        assert (
            warehouse.load_object(
                recreated, byte_size=100, batch=ScreenTimeBatch([_record(recreated)])
            )
            == 1
        )

        assert warehouse.succeeded_keys(source_id="screen_time", stream="App.InFocus") == {
            original.key
        }
        assert warehouse.query_rows(
            """
            SELECT storage_created_at, retention_expired_at
            FROM ops.ingestion_metadata
            WHERE object_key = ?
            """,
            [original.key],
        ) == [(recreated.storage_created_at, None)]
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    finally:
        warehouse.close()


def test_new_generation_is_not_trusted_until_it_is_reloaded(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "test.duckdb"))))
    try:
        warehouse.migrate()
        original = _raw()
        assert (
            warehouse.load_object(
                original, byte_size=100, batch=ScreenTimeBatch([_record(original)])
            )
            == 1
        )
        recreated_at = datetime(2026, 10, 27, 1, tzinfo=UTC)
        recreated = _raw(storage_created_at=recreated_at, storage_generation=2)

        assert warehouse.succeeded_keys_for([recreated]) == set()
        assert (
            warehouse.load_object(
                recreated, byte_size=100, batch=ScreenTimeBatch([_record(recreated)])
            )
            == 1
        )

        assert warehouse.succeeded_keys_for([recreated]) == {original.key}
        assert warehouse.query_rows(
            """
            SELECT storage_created_at, storage_generation, retention_expired_at
            FROM ops.ingestion_metadata
            """
        ) == [(recreated_at, 2, None)]
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    finally:
        warehouse.close()


def test_job_lease_rejects_a_second_owner(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "test.duckdb"))))
    try:
        warehouse.migrate()
        assert warehouse.acquire_job_lock("loader", "first", lease_seconds=60)
        assert not warehouse.acquire_job_lock("loader", "second", lease_seconds=60)

        warehouse.release_job_lock("loader", "not-the-owner")
        assert not warehouse.acquire_job_lock("loader", "second", lease_seconds=60)

        warehouse.release_job_lock("loader", "first")
        assert warehouse.acquire_job_lock("loader", "second", lease_seconds=60)
    finally:
        warehouse.close()


@dataclass(frozen=True)
class _MetricBatch:
    amount: int = 7
    fail: bool = False
    parser_version: str = "fixture-metric-v1"
    record_count: int = 1

    def write(self, connection, raw, *, byte_size, loaded_at) -> None:
        connection.execute("DELETE FROM base.fixture_metric WHERE object_key = ?", [raw.key])
        connection.execute(
            "INSERT INTO base.fixture_metric VALUES (?, ?, ?)",
            [raw.key, self.amount, loaded_at],
        )
        if self.fail:
            raise ValueError("source writer failed after inserting a row")


def _metric_warehouse() -> Warehouse:
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    warehouse.connection.execute(
        """
        CREATE TABLE base.fixture_metric (
            object_key VARCHAR PRIMARY KEY, amount INTEGER NOT NULL, loaded_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    return warehouse


def test_ingestion_queries_isolate_both_source_and_stream() -> None:
    warehouse = _metric_warehouse()
    try:
        screen_time = _raw()
        other_stream = replace(screen_time, key="raw/other-stream", stream="other-stream")
        other_source = replace(screen_time, key="raw/other-source", source_id="fixture_health")
        warehouse.load_object(screen_time, byte_size=10, batch=_MetricBatch())
        warehouse.mark_failed(other_stream, byte_size=0, error=ValueError("other stream"))
        warehouse.load_object(other_source, byte_size=10, batch=_MetricBatch())
        scope = {"source_id": screen_time.source_id, "stream": screen_time.stream}
        assert warehouse.succeeded_keys(**scope) == {screen_time.key}
        assert warehouse.ingestion_counts(**scope) == {"succeeded": 1}
        assert set(warehouse.active_ingestion_states(**scope)) == {screen_time.key}
        assert warehouse.ingestion_counts(
            source_id=other_stream.source_id, stream=other_stream.stream
        ) == {"failed": 1}
    finally:
        warehouse.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [("source_id", "fixture_health"), ("stream", "other-stream"), ("schema_version", 2)],
)
def test_object_identity_cannot_cross_sources_streams_or_versions(field, value) -> None:
    warehouse = _metric_warehouse()
    try:
        raw = _raw()
        warehouse.load_object(raw, byte_size=10, batch=_MetricBatch())
        mismatched = replace(raw, **{field: value})
        assert warehouse.succeeded_keys_for([mismatched]) == set()
        with pytest.raises(RuntimeError, match="immutable object identity changed"):
            warehouse.load_object(mismatched, byte_size=10, batch=_MetricBatch())
        with pytest.raises(RuntimeError, match="immutable object identity changed"):
            warehouse.mark_failed(mismatched, byte_size=0, error=ValueError("decode failed"))
        assert warehouse.succeeded_keys_for([raw]) == {raw.key}
        assert warehouse.query_value("SELECT status FROM ops.ingestion_metadata") == "succeeded"
    finally:
        warehouse.close()


def test_a_non_screen_time_batch_writes_typed_rows_with_source_identity() -> None:
    warehouse = _metric_warehouse()
    try:
        raw = replace(
            _raw(), key="raw/fixture-health", source_id="fixture_health", subject_key="account"
        )
        assert warehouse.load_object(raw, byte_size=10, batch=_MetricBatch(amount=42)) == 1
        assert warehouse.query_value("SELECT amount FROM base.fixture_metric") == 42
        assert warehouse.query_rows(
            """
            SELECT source_id, subject_key, logical_key, parser_version
            FROM ops.ingestion_metadata
            """
        ) == [("fixture_health", "account", raw.logical_key, "fixture-metric-v1")]
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 0
    finally:
        warehouse.close()


@pytest.mark.parametrize("previously_loaded", [False, True])
def test_source_writer_failure_rolls_back_records_and_object_state(previously_loaded) -> None:
    warehouse = _metric_warehouse()
    try:
        original = _raw()
        if previously_loaded:
            warehouse.load_object(original, byte_size=10, batch=_MetricBatch(amount=1))
        attempted = replace(original, storage_generation=2)
        with pytest.raises(ValueError, match="source writer failed") as failure:
            warehouse.load_object(attempted, byte_size=10, batch=_MetricBatch(amount=2, fail=True))

        assert warehouse.query_rows("SELECT amount FROM base.fixture_metric") == (
            [(1,)] if previously_loaded else []
        )
        assert warehouse.query_rows(
            "SELECT status, storage_generation FROM ops.ingestion_metadata"
        ) == ([("succeeded", 1)] if previously_loaded else [])

        warehouse.mark_failed(attempted, byte_size=10, error=failure.value)
        assert warehouse.query_rows(
            "SELECT status, storage_generation FROM ops.ingestion_metadata"
        ) == [("failed", 2)]
        assert warehouse.query_rows("SELECT amount FROM base.fixture_metric") == (
            [(1,)] if previously_loaded else []
        )
    finally:
        warehouse.close()


def test_empty_screen_time_batch_keeps_its_decoder_version_and_source_identity() -> None:
    warehouse = _metric_warehouse()
    try:
        raw = _raw()
        assert (
            warehouse.load_object(
                raw,
                byte_size=10,
                batch=ScreenTimeBatch([]),
            )
            == 0
        )
        assert warehouse.query_rows(
            """
            SELECT parser_version, record_count, subject_key, logical_key
            FROM ops.ingestion_metadata
            """
        ) == [(PARSER_VERSION, 0, raw.subject_key, raw.logical_key)]
        assert warehouse.query_rows(
            "SELECT device_key, segment_key FROM ops.screen_time_segment"
        ) == [(raw.subject_key, raw.logical_key)]
        failed_raw = replace(raw, key="raw/failed")
        warehouse.mark_failed(
            failed_raw,
            byte_size=0,
            error=ValueError("decode failed"),
        )
        assert warehouse.query_rows(
            "SELECT subject_key, logical_key FROM ops.ingestion_metadata WHERE object_key = ?",
            [failed_raw.key],
        ) == [(raw.subject_key, raw.logical_key)]
    finally:
        warehouse.close()

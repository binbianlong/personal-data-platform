from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

import pytest

from personal_data_platform.reconciliation.models import ReconciliationResult
from personal_data_platform.sources.screen_time.parser import PARSER_VERSION
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import (
    Warehouse,
    WarehouseConfig,
    connect,
)
from tests.screen_time_helpers import _raw, _record


@pytest.mark.parametrize("as_mapping", [False, True])
def test_reconciliation_accepts_result_and_dictionary(as_mapping) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    now = datetime(2026, 9, 23, tzinfo=UTC)
    result = ReconciliationResult(
        run_id="typed-audit",
        status="succeeded",
        started_at=now,
        completed_at=now,
        raw_object_count=3,
        loaded_object_count=3,
        missing_object_count=0,
        failed_object_count=0,
        orphaned_loaded_object_count=0,
        details={"collector_receipt_count": 2},
    )
    try:
        warehouse.migrate()
        warehouse.record_reconciliation(asdict(result) if as_mapping else result)
        row = warehouse.query_rows(
            "SELECT run_id, status, raw_object_count, loaded_object_count, details "
            "FROM ops.reconciliation_run"
        )[0]
        assert row[:4] == ("typed-audit", "succeeded", 3, 3)
        assert json.loads(row[4]) == {"collector_receipt_count": 2}
    finally:
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
        original_state = warehouse.active_ingestion_states(
            source_id="screen_time", stream="App.InFocus"
        )[original.key]
        assert warehouse.mark_retention_expired([original_state], expired_at=datetime.now(UTC)) == {
            original.key
        }
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


@pytest.mark.parametrize(
    ("changed_column", "replacement"),
    [
        ("storage_created_at", datetime(2026, 10, 27, 1, tzinfo=UTC)),
        ("storage_generation", 2),
    ],
)
def test_changed_storage_identity_cannot_be_marked_as_retention_expired(
    tmp_path, changed_column: str, replacement: object
) -> None:
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
        stale_state = warehouse.active_ingestion_states(
            source_id="screen_time", stream="App.InFocus"
        )[original.key]

        warehouse.connection.execute(
            f"UPDATE ops.ingestion_metadata SET {changed_column} = ? WHERE object_key = ?",
            [replacement, original.key],
        )

        assert (
            warehouse.mark_retention_expired([stale_state], expired_at=datetime.now(UTC)) == set()
        )
        assert (
            warehouse.query_value(
                f"SELECT {changed_column} FROM ops.ingestion_metadata WHERE object_key = ?",
                [original.key],
            )
            == replacement
        )
        assert (
            warehouse.query_value(
                "SELECT retention_expired_at FROM ops.ingestion_metadata WHERE object_key = ?",
                [original.key],
            )
            is None
        )
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
        other_states = warehouse.active_ingestion_states(
            source_id=other_source.source_id, stream=other_source.stream
        )
        warehouse.mark_retention_expired(other_states.values(), expired_at=datetime.now(UTC))

        scope = {"source_id": screen_time.source_id, "stream": screen_time.stream}
        assert warehouse.succeeded_keys(**scope) == {screen_time.key}
        assert warehouse.ingestion_counts(**scope) == {"succeeded": 1}
        assert set(warehouse.active_ingestion_states(**scope)) == {screen_time.key}
        assert warehouse.retention_inventory_counts(**scope) == {
            "total_object_count": 1,
            "expired_object_count": 0,
        }
        assert warehouse.ingestion_counts(
            source_id=other_stream.source_id, stream=other_stream.stream
        ) == {"failed": 1}
        assert warehouse.retention_inventory_counts(
            source_id=other_source.source_id, stream=other_source.stream
        ) == {"total_object_count": 1, "expired_object_count": 1}
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
            "SELECT device_key, segment_key, source_segment_names FROM ops.screen_time_segment"
        ) == [(raw.subject_key, raw.logical_key, [])]
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

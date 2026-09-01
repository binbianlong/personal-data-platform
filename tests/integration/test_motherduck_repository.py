from __future__ import annotations

from datetime import UTC, datetime

import pytest

from personal_data_platform.loader.models import ParsedScreenTimeRecord, RawObject
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


def _raw(
    *,
    storage_created_at: datetime = datetime(2026, 8, 27, 1, tzinfo=UTC),
    storage_generation: int = 1,
) -> RawObject:
    return RawObject(
        key="raw/screen_time/v1/device/App.InFocus/segment/2026-08-27T00:00:00Z/hash.segb.gz",
        device_key="device",
        stream="App.InFocus",
        segment_key="segment",
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        sha256="a" * 64,
        storage_created_at=storage_created_at,
        storage_generation=storage_generation,
    )


def _record(raw: RawObject) -> ParsedScreenTimeRecord:
    return ParsedScreenTimeRecord(
        event_key="event",
        object_key=raw.key,
        device_key=raw.device_key,
        source_stream=raw.stream,
        segment_key=raw.segment_key,
        segment_sha256=raw.sha256,
        observed_at=raw.observed_at,
        segment_filename="segment.segb",
        record_offset=12,
        record_metadata_offset=120,
        record_state="WRITTEN",
        segment_record_timestamp=raw.observed_at,
        crc_passed=True,
        transition_reason="foreground",
        kind=1,
        in_foreground=True,
        cf_absolute_time=1.0,
        event_at=datetime(2001, 1, 1, 0, 0, 1, tzinfo=UTC),
        bundle_id="com.example.app",
        app_version="1",
        app_build="1",
        platform_flag=2,
        unknown_field_count=0,
        original_payload=b"payload",
        parser_version="app-in-focus-v1",
    )


def test_migration_and_object_load_are_idempotent(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "test.duckdb"))))
    try:
        warehouse.migrate()
        raw = _raw()

        assert warehouse.load_object(raw, byte_size=100, records=[_record(raw)]) == 1
        assert warehouse.load_object(raw, byte_size=100, records=[_record(raw)]) == 0
        assert warehouse.succeeded_keys() == {raw.key}
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_record_occurrence") == 1
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

        assert warehouse.ingestion_counts() == {"failed": 1}
        assert (
            warehouse.query_value("SELECT storage_created_at FROM ops.ingestion_metadata")
            == raw.storage_created_at
        )
        assert warehouse.load_object(raw, byte_size=100, records=[_record(raw)]) == 1
        assert warehouse.ingestion_counts() == {"succeeded": 1}
    finally:
        warehouse.close()


def test_expired_key_can_be_reloaded_with_a_new_storage_creation_time(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "test.duckdb"))))
    try:
        warehouse.migrate()
        original = _raw()
        assert warehouse.load_object(original, byte_size=100, records=[_record(original)]) == 1
        original_state = warehouse.active_ingestion_states()[original.key]
        assert warehouse.mark_retention_expired([original_state], expired_at=datetime.now(UTC)) == {
            original.key
        }
        assert warehouse.succeeded_keys() == set()

        recreated = _raw(
            storage_created_at=datetime(2026, 10, 27, 1, tzinfo=UTC),
            storage_generation=2,
        )
        assert warehouse.load_object(recreated, byte_size=100, records=[_record(recreated)]) == 1

        assert warehouse.succeeded_keys() == {original.key}
        assert warehouse.query_rows(
            """
            SELECT storage_created_at, retention_expired_at
            FROM ops.ingestion_metadata
            WHERE object_key = ?
            """,
            [original.key],
        ) == [(recreated.storage_created_at, None)]
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_record_occurrence") == 1
    finally:
        warehouse.close()


def test_new_generation_is_not_trusted_until_it_is_reloaded(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "test.duckdb"))))
    try:
        warehouse.migrate()
        original = _raw()
        assert warehouse.load_object(original, byte_size=100, records=[_record(original)]) == 1
        recreated_at = datetime(2026, 10, 27, 1, tzinfo=UTC)
        recreated = _raw(storage_created_at=recreated_at, storage_generation=2)

        assert warehouse.succeeded_keys_for([recreated]) == set()
        assert warehouse.load_object(recreated, byte_size=100, records=[_record(recreated)]) == 1

        assert warehouse.succeeded_keys_for([recreated]) == {original.key}
        assert warehouse.query_rows(
            """
            SELECT storage_created_at, storage_generation, retention_expired_at
            FROM ops.ingestion_metadata
            """
        ) == [(recreated_at, 2, None)]
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_record_occurrence") == 1
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
        assert warehouse.load_object(original, byte_size=100, records=[_record(original)]) == 1
        stale_state = warehouse.active_ingestion_states()[original.key]

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

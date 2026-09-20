from __future__ import annotations

import gzip
import shutil

import duckdb
import pytest

from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.storage.motherduck import (
    DEFAULT_MIGRATIONS,
    Warehouse,
    WarehouseConfig,
    connect,
)
from tests.legacy_screen_time import LegacyScreenTimeBatch
from tests.screen_time_helpers import Repository, event, segb, tombstone


@pytest.mark.parametrize("reason", [1, 2])
@pytest.mark.parametrize("fail_first", [False, True])
def test_upgrade_repairs_deletion_from_incomplete_historical_names(tmp_path, reason, fail_first):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in DEFAULT_MIGRATIONS.glob("00[1-5]*.sql"):
        shutil.copyfile(path, migrations / path.name)
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    repository = Repository()
    payload = event("app.target")
    segment, offset = segb(payload)
    repository.add("100", segb(event("app.conflict"))[0], logical="b" * 64)
    repository.add("200", segb(event("app.conflict"))[0], logical="b" * 64)
    repository.add("200", segment, logical="c" * 64)
    repository.add(
        "300", segb(tombstone("200", offset, len(payload), reason=reason))[0], kind="tombstones"
    )
    # An unrelated device's unambiguous deletion must remain applied.
    repository.add("200", segment, device="d" * 64)
    repository.add(
        "300",
        segb(tombstone("200", offset, len(payload), reason=reason))[0],
        kind="tombstones",
        device="d" * 64,
    )
    if reason == 1:
        erased = segb(b"\0" * len(payload), state=3, crc=123)[0]
        repository.add("200", erased, logical="c" * 64)
        repository.add("200", erased, device="d" * 64)
    try:
        warehouse.migrate(migrations)
        for raw, content in repository.objects.values():
            batch = ScreenTimeSource().decode(raw, gzip.decompress(content))
            warehouse.load_object(
                raw,
                byte_size=1,
                batch=LegacyScreenTimeBatch(
                    batch.records,
                    batch.source_segment_name,
                    batch.segment_kind,
                ),
            )
        for path in DEFAULT_MIGRATIONS.glob("00[6-7]*.sql"):
            shutil.copyfile(path, migrations / path.name)
        warehouse.migrate(migrations)
        assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_deletion_match") == 2
        receipts = warehouse.query_rows("SELECT * FROM ops.ingestion_metadata ORDER BY object_key")
        unaffected = warehouse.query_rows(
            "SELECT * FROM base.screen_time_event WHERE device_key=?", ["d" * 64]
        )
        # Simulate the current runtime: only the first name survived in ops;
        # archived observations cannot be assumed to contain later names.
        warehouse.connection.execute("DELETE FROM base.screen_time_record_occurrence")
        warehouse.connection.execute("DELETE FROM base.screen_time_segment_observation")
        if fail_first:
            tables = (
                "base.screen_time_event",
                "ops.screen_time_segment",
                "ops.screen_time_tombstone",
                "ops.screen_time_deletion_match",
                "ops.schema_migration",
            )
            before_failure = {
                table: warehouse.query_rows(f"SELECT * FROM {table} ORDER BY ALL")
                for table in tables
            }
            failing = tmp_path / "failing"
            failing.mkdir()
            migration = DEFAULT_MIGRATIONS / "008_screen_time_segment_names.sql"
            (failing / migration.name).write_text(
                migration.read_text() + "\nSELECT error('interrupted repair');"
            )
            with pytest.raises(duckdb.InvalidInputException, match="interrupted repair"):
                warehouse.migrate(failing)
            assert {
                table: warehouse.query_rows(f"SELECT * FROM {table} ORDER BY ALL")
                for table in tables
            } == before_failure
        warehouse.migrate()
        assert warehouse.query_rows(
            "SELECT device_key, is_active FROM base.screen_time_event "
            "WHERE bundle_id='app.target' ORDER BY device_key"
        ) == [("a" * 64, reason == 2), ("d" * 64, reason == 1)]
        assert (
            warehouse.query_rows(
                "SELECT * FROM base.screen_time_event WHERE device_key=?", ["d" * 64]
            )
            == unaffected
        )
        assert (
            warehouse.query_rows("SELECT * FROM ops.ingestion_metadata ORDER BY object_key")
            == receipts
        )
        assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_deletion_match") == 1
        # A newly observed name cannot reconstruct the lost historical aliases.
        raw = repository.add("100", segb(event("app.conflict"))[0], logical="b" * 64)
        batch = ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))
        warehouse.load_object(raw, byte_size=1, batch=batch)
        assert (
            warehouse.query_value(
                "SELECT source_segment_names FROM ops.screen_time_segment WHERE segment_key=?",
                ["b" * 64],
            )
            is None
        )
        assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_deletion_match") == 1
        before = warehouse.query_rows("SELECT * FROM base.screen_time_event ORDER BY event_key")
        warehouse.migrate()
        assert (
            warehouse.query_rows("SELECT * FROM base.screen_time_event ORDER BY event_key")
            == before
        )
    finally:
        warehouse.close()

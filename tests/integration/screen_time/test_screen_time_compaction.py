"""Current Screen Time state remains small while analytical history survives."""

from __future__ import annotations

import gzip
import shutil
from dataclasses import replace

import duckdb
import pytest

from personal_data_platform.loader.job import run_loader
from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.migrations import DEFAULT_MIGRATIONS
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import Repository, event, segb


def _warehouse(path):
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    return warehouse


def _load(warehouse, repository, raw):
    batch = ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))
    warehouse.load_object(raw, byte_size=1, batch=batch)
    return batch


def test_latest_snapshot_replaces_operational_rows_and_preserves_history(tmp_path):
    warehouse = _warehouse(tmp_path / "events.duckdb")
    repository = Repository()
    first = repository.add("100", segb(event("app.a"))[0])
    second = repository.add("100", segb(event("app.b"))[0])
    third = repository.add("100", segb(event("app.a"))[0])
    for raw in (first, second, third):
        _load(warehouse, repository, raw)
    assert warehouse.query_rows(
        "SELECT bundle_id, is_active FROM base.screen_time_event ORDER BY bundle_id"
    ) == [("app.a", True), ("app.b", False)]
    assert warehouse.query_rows("SELECT bundle_id FROM ops.screen_time_record") == [("app.a",)]
    _load(warehouse, repository, second)
    assert warehouse.query_rows("SELECT bundle_id FROM ops.screen_time_record") == [("app.a",)]
    warehouse.close()


def test_empty_latest_snapshot_and_late_reparse_cannot_restore_records(tmp_path):
    warehouse = _warehouse(tmp_path / "events.duckdb")
    repository = Repository()
    first = repository.add("100", segb(event("app.a"))[0])
    empty = repository.add("100", b"")
    first_batch = _load(warehouse, repository, first)
    warehouse.load_object(empty, byte_size=0, batch=ScreenTimeBatch([], "100", "events"))
    assert warehouse.query_rows("SELECT is_active FROM base.screen_time_event") == [(False,)]
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == 0
    warehouse.load_object(
        first,
        byte_size=1,
        batch=replace(
            first_batch, records=[replace(r, parser_version="next") for r in first_batch.records]
        ),
    )
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == 0
    assert warehouse.query_rows("SELECT is_active FROM base.screen_time_event") == [(False,)]
    warehouse.close()


def test_tombstone_input_does_not_suppress_current_event(tmp_path):
    warehouse = _warehouse(tmp_path / "events.duckdb")
    repository = Repository()
    payload = event("app.a")
    segment, _ = segb(payload)
    _load(warehouse, repository, repository.add("100", segment))
    deletion = repository.add(
        "100", segb(b"unknown deletion marker")[0], kind="tombstones"
    )
    _load(warehouse, repository, deletion)
    assert warehouse.query_rows("SELECT is_active FROM base.screen_time_event") == [(True,)]
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == 1
    warehouse.close()


def _legacy_warehouse(tmp_path):
    old_migrations = tmp_path / "old_migrations"
    old_migrations.mkdir()
    for name in ("001_initial.sql", "002_owntracks.sql"):
        shutil.copyfile(DEFAULT_MIGRATIONS / name, old_migrations / name)
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "old.duckdb"))))
    warehouse.migrate(old_migrations)
    warehouse.connection.execute("""
        INSERT INTO ops.screen_time_segment VALUES
            ('device', 'app-in-focus', 'segment', TIMESTAMPTZ '2026-10-01 00:00:00+00',
             'raw/new', '100', false, ['100']);
        INSERT INTO ops.screen_time_record VALUES
            ('old', 'device', 'app-in-focus', 'segment', 32, 48, 20, 1.0,
             'old-event', 'app.old', TIMESTAMPTZ '2026-10-01 00:00:00+00',
             'start', NULL, NULL, NULL, NULL, NULL, 'parser', 0, '100',
             TIMESTAMPTZ '2026-09-30 00:00:00+00', 'raw/old', false, true),
            ('new', 'device', 'app-in-focus', 'segment', 64, 80, 20, 2.0,
             'new-event', 'app.new', TIMESTAMPTZ '2026-10-01 00:01:00+00',
             'start', NULL, NULL, NULL, NULL, NULL, 'parser', 0, '100',
             TIMESTAMPTZ '2026-10-01 00:00:00+00', 'raw/new', true, true);
        INSERT INTO base.screen_time_event
        SELECT *, current_timestamp FROM ops.screen_time_resolve(['old-event','new-event']);
    """)
    return warehouse


def test_forward_migration_keeps_base_history_and_drops_old_state(tmp_path):
    warehouse = _legacy_warehouse(tmp_path)
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == 2
    before = warehouse.query_rows(
        "SELECT bundle_id, is_active FROM base.screen_time_event ORDER BY bundle_id"
    )
    before_view = warehouse.query_rows("SELECT bundle_id FROM base.screen_time_transition")
    warehouse.migrate()
    warehouse.migrate()
    assert (
        warehouse.query_rows(
            "SELECT bundle_id, is_active FROM base.screen_time_event ORDER BY bundle_id"
        )
        == before
    )
    assert warehouse.query_rows("SELECT bundle_id FROM base.screen_time_transition") == before_view
    assert warehouse.query_rows("SELECT bundle_id FROM ops.screen_time_record") == [("app.new",)]
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='ops' "
            "AND table_name IN ('screen_time_tombstone','screen_time_deletion_match')"
        )
        == 0
    )
    warehouse.close()


def test_forward_migration_failure_rolls_back_existing_state(tmp_path):
    warehouse = _legacy_warehouse(tmp_path)
    before = warehouse.query_rows("SELECT * FROM ops.screen_time_record ORDER BY physical_id")
    failing = tmp_path / "failing_migrations"
    failing.mkdir()
    for migration in DEFAULT_MIGRATIONS.glob("*.sql"):
        shutil.copyfile(migration, failing / migration.name)
    newest = failing / "003_screen_time_current_state.sql"
    newest.write_text(newest.read_text() + "\nSELECT error('interrupted conversion');\n")
    with pytest.raises(duckdb.InvalidInputException, match="interrupted conversion"):
        warehouse.migrate(failing)
    assert (
        warehouse.query_rows("SELECT * FROM ops.screen_time_record ORDER BY physical_id") == before
    )
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_tombstone") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.schema_migration") == 2
    warehouse.migrate()
    assert warehouse.query_rows("SELECT bundle_id FROM ops.screen_time_record") == [("app.new",)]
    warehouse.close()


def test_forward_migration_removes_previous_deletion_effect(tmp_path):
    warehouse = _legacy_warehouse(tmp_path)
    warehouse.connection.execute("""
        INSERT INTO ops.screen_time_tombstone VALUES
            ('deletion', 'device', 'app-in-focus', 'tombstone-segment', '100',
             80, 20, 2.0, 2, current_timestamp, 'raw/deletion', true,
             'user_deletion_applied');
        INSERT INTO ops.screen_time_deletion_match VALUES ('deletion', 'new');
        UPDATE base.screen_time_event SET is_active = false WHERE event_key = 'new-event';
    """)
    warehouse.migrate()
    assert warehouse.query_rows(
        "SELECT bundle_id, is_active FROM base.screen_time_event ORDER BY bundle_id"
    ) == [("app.new", True), ("app.old", False)]
    warehouse.close()


def test_parser_upgrade_reprocesses_current_raw():
    repository = Repository()
    raw = repository.add("100", segb(event("app.upgrade"))[0])
    source = ScreenTimeSource()
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate()
        batch = source.decode(raw, gzip.decompress(repository.objects[raw.key][1]))
        older = replace(
            batch, records=[replace(r, parser_version="app-in-focus-v1") for r in batch.records]
        )
        warehouse.load_object(raw, byte_size=1, batch=older)
        assert run_loader(repository, warehouse).succeeded == 1
        assert run_loader(repository, warehouse).skipped == 1
        assert (
            warehouse.query_value("SELECT parser_version FROM ops.screen_time_record")
            == source.parser_version
        )
        assert warehouse.query_rows("SELECT is_active FROM base.screen_time_event") == [(True,)]
    finally:
        warehouse.close()


def test_reparse_correction_deactivates_old_key_and_replaces_current_record(tmp_path):
    warehouse = _warehouse(tmp_path / "events.duckdb")
    repository = Repository()
    raw = repository.add("100", segb(event("app.original"))[0])
    batch = _load(warehouse, repository, raw)
    warehouse.load_object(
        raw,
        byte_size=1,
        batch=replace(
            batch,
            records=[
                replace(r, event_key="corrected", bundle_id="app.corrected", parser_version="next")
                for r in batch.records
            ],
        ),
    )
    assert warehouse.query_rows(
        "SELECT bundle_id, is_active FROM base.screen_time_event ORDER BY bundle_id"
    ) == [("app.corrected", True), ("app.original", False)]
    assert warehouse.query_rows("SELECT event_key FROM ops.screen_time_record") == [("corrected",)]
    warehouse.close()

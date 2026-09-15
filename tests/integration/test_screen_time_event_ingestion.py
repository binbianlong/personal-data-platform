from __future__ import annotations

import gzip
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.loader.job import run_loader
from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import (
    DEFAULT_MIGRATIONS,
    Warehouse,
    WarehouseConfig,
    WarehouseConnectionError,
    connect,
)
from tests.legacy_screen_time import LegacyScreenTimeBatch
from tests.screen_time_helpers import Repository, event, segb, tombstone


def decode(repository, raw):
    return ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))


def warehouse_at(path):
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    return warehouse


def test_reobservations_store_one_event_and_only_state_changes(tmp_path):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    initial = repository.add("100", segb(event("app.once"))[0])
    initial_batch = decode(repository, initial)
    warehouse.load_object(initial, byte_size=1, batch=initial_batch)
    original = warehouse.query_rows("SELECT * FROM base.screen_time_event")
    initial_counts = state_counts(warehouse)
    for index in range(1, 20):
        raw = replace(
            initial,
            key=initial.key + str(index),
            observed_at=initial.observed_at + timedelta(seconds=index),
        )
        batch = replace(
            initial_batch,
            records=[
                replace(record, object_key=raw.key, observed_at=raw.observed_at)
                for record in initial_batch.records
            ],
        )
        assert warehouse.load_object(raw, byte_size=1, batch=batch) == 1
        assert warehouse.load_object(raw, byte_size=1, batch=batch) == 0
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event") == original
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_record_occurrence") == 0
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_segment_observation") == 0
    assert state_counts(warehouse) == initial_counts
    assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 20
    assert not warehouse.query_rows(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'ops' "
        "AND table_name LIKE 'screen_time_%' AND column_name IN ('original_payload', 'document')"
    )
    warehouse.close()
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event") == original
    warehouse.close()


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 0, 1), (1, 2, 0)])
def test_out_of_order_and_reparse_keep_later_snapshot(tmp_path, order):
    repository = Repository()
    raws = [repository.add("100", segb(event(f"app.{index}"))[0]) for index in range(3)]
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    for index in order:
        raw = raws[index]
        warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))
    assert warehouse.query_rows("SELECT bundle_id FROM base.screen_time_event WHERE is_active") == [
        ("app.2",)
    ]
    old = decode(repository, raws[0])
    warehouse.load_object(
        raws[0],
        byte_size=1,
        batch=replace(
            old, records=[replace(record, parser_version="next-parser") for record in old.records]
        ),
    )
    assert warehouse.query_rows("SELECT bundle_id FROM base.screen_time_event WHERE is_active") == [
        ("app.2",)
    ]
    warehouse.close()


def test_cutover_keeps_old_rows_and_user_deletion_overrides_legacy(tmp_path, monkeypatch):
    database = tmp_path / "events.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    migrations = tmp_path / "legacy_migrations"
    migrations.mkdir()
    for path in DEFAULT_MIGRATIONS.glob("00[1-5]*.sql"):
        shutil.copyfile(path, migrations / path.name)
    warehouse.migrate(migrations)
    repository = Repository()
    payload = event("app.legacy")
    segment, offset = segb(payload)
    raw = repository.add("100", segment)
    batch = decode(repository, raw)
    warehouse.load_object(
        raw,
        byte_size=1,
        batch=LegacyScreenTimeBatch(batch.records, batch.source_segment_name, batch.segment_kind),
    )
    legacy_rows = warehouse.query_rows("SELECT * FROM base.screen_time_record_occurrence")
    warehouse.migrate()
    warehouse.migrate()
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    # A different segment contains the same event. No extra event row is created on retry.
    copy = repository.add("200", segment)
    warehouse.load_object(copy, byte_size=1, batch=decode(repository, copy))
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    deletion = repository.add(
        "100", segb(tombstone("100", offset, len(payload)))[0], kind="tombstones", logical="f" * 64
    )
    warehouse.load_object(deletion, byte_size=1, batch=decode(repository, deletion))
    assert warehouse.query_rows("SELECT is_active FROM base.screen_time_event") == [(False,)]
    assert warehouse.query_rows("SELECT * FROM base.screen_time_record_occurrence") == legacy_rows
    warehouse.close()
    project = tmp_path / "dbt"
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "dbt",
        project,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages", ".user.yml"),
    )
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=project)
    warehouse = warehouse_at(database)
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_legacy_transition") == 1
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_transition") == 0
    assert warehouse.query_value("SELECT count(*) FROM marts.daily_screen_time") == 0
    warehouse.close()


STATE_TABLES = ("segment", "record", "tombstone", "deletion_match")


def state_counts(warehouse):
    return {
        t: warehouse.query_value(f"SELECT count(*) FROM ops.screen_time_{t}") for t in STATE_TABLES
    }


def load(warehouse, repository, raw):
    return warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))


@pytest.mark.parametrize("unrelated", [0, 25])
def test_unrelated_segments_do_not_expand_working_sets(tmp_path, unrelated):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    for i in range(unrelated):
        load(warehouse, repository, repository.add(str(200 + i), segb(event(f"other.{i}"))[0]))
    first = repository.add("100", segb(event("app.first"))[0])
    load(warehouse, repository, first)
    counts = state_counts(warehouse)
    load(warehouse, repository, repository.add("100", segb(event("app.first"))[0]))
    assert state_counts(warehouse) == counts
    assert warehouse.query_value("SELECT count(*) FROM screen_time_affected_event") == 1
    assert warehouse.query_value("SELECT count(*) FROM screen_time_affected_tombstone") == 0
    warehouse.close()


@pytest.mark.parametrize("reason", [1, 2])
@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 0, 2)])
def test_deletion_target_late_and_empty_snapshot(tmp_path, reason, order):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    payload = event("app.deleted")
    segment, offset = segb(payload)
    raws = [
        repository.add("100", segment),
        repository.add("100", b""),
        repository.add(
            "100", segb(tombstone("100", offset, len(payload), reason=reason))[0], kind="tombstones"
        ),
    ]
    for i in order:
        if i == 1:
            warehouse.load_object(raws[i], byte_size=0, batch=ScreenTimeBatch([], "100", "events"))
        else:
            load(warehouse, repository, raws[i])
    assert warehouse.query_rows("SELECT is_active FROM base.screen_time_event") == [(reason == 1,)]
    counts = state_counts(warehouse)
    baseline = warehouse.query_rows("SELECT * FROM base.screen_time_event")
    for i in range(5):
        repeated = repository.add(
            "100", segb(tombstone("100", offset, len(payload), reason=reason))[0], kind="tombstones"
        )
        load(warehouse, repository, repeated)
    assert state_counts(warehouse) == counts
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event") == baseline
    warehouse.close()


def test_reparse_empty_latest_and_older_snapshot(tmp_path):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    raws = [repository.add("100", segb(event(f"app.{i}"))[0]) for i in range(3)]
    for raw in raws:
        load(warehouse, repository, raw)
    warehouse.load_object(raws[1], byte_size=0, batch=ScreenTimeBatch([], "100", "events"))
    assert warehouse.query_rows("SELECT bundle_id FROM base.screen_time_event WHERE is_active") == [
        ("app.2",)
    ]
    warehouse.load_object(raws[2], byte_size=0, batch=ScreenTimeBatch([], "100", "events"))
    # Empty batches use the source parser version, so force a reparse using the receipt.
    warehouse.connection.execute("UPDATE ops.ingestion_metadata SET parser_version = 'old'")
    warehouse.load_object(raws[2], byte_size=0, batch=ScreenTimeBatch([], "100", "events"))
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event WHERE is_active") == 0
    warehouse.close()


def test_reparse_tombstone_and_event_corrects_existing_physical_rows(tmp_path):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    payload = event("app.original")
    segment, offset = segb(payload)
    raw = repository.add("100", segment)
    load(warehouse, repository, raw)
    deletion = repository.add(
        "100", segb(tombstone("100", offset, len(payload)))[0], kind="tombstones"
    )
    load(warehouse, repository, deletion)
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is False
    batch = decode(repository, deletion)
    warehouse.load_object(
        deletion,
        byte_size=1,
        batch=replace(
            batch,
            records=[replace(r, deletion_reason=99, parser_version="next") for r in batch.records],
        ),
    )
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is True
    batch = decode(repository, raw)
    warehouse.load_object(
        raw,
        byte_size=1,
        batch=replace(
            batch,
            records=[
                replace(r, bundle_id="app.corrected", event_key="corrected", parser_version="next")
                for r in batch.records
            ],
        ),
    )
    assert warehouse.query_rows("SELECT bundle_id FROM base.screen_time_event WHERE is_active") == [
        ("app.corrected",)
    ]
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == 1
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_tombstone") == 1
    warehouse.close()


class InterruptedConnection:
    def __init__(self, connection, stage):
        self.connection = connection
        self.stage = stage
        self.failed = False
        self.calls_after_failure = []

    def execute(self, sql, *args):
        if self.failed:
            self.calls_after_failure.append(sql)
        match = (
            (self.stage in ("commit_before", "commit_after") and sql == "COMMIT")
            or (self.stage == "success" and "SET status = 'succeeded'" in sql)
            or (self.stage == "events" and "INSERT INTO base.screen_time_event" in sql)
            or (self.stage == "state" and "INSERT INTO ops.screen_time_tombstone" in sql)
        )
        if match and not self.failed:
            self.failed = True
            if self.stage == "commit_after":
                self.connection.execute(sql, *args)
            raise OSError("database response lost")
        return self.connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize("stage", ["state", "events", "success", "commit_before", "commit_after"])
def test_transaction_failure_and_reconnect_retry(tmp_path, stage):
    path = tmp_path / "events.duckdb"
    warehouse = warehouse_at(path)
    repository = Repository()
    raw = repository.add("100", segb(event("app.once"))[0])
    wrapper = InterruptedConnection(warehouse.connection, stage)
    warehouse.connection = wrapper
    exception = WarehouseConnectionError if stage.startswith("commit") else OSError
    with pytest.raises(exception):
        load(warehouse, repository, raw)
    if stage.startswith("commit"):
        with pytest.raises(WarehouseConnectionError):
            load(warehouse, repository, raw)
        with pytest.raises(WarehouseConnectionError):
            warehouse.mark_failed(raw, byte_size=1, error=ValueError("bad"))
        assert wrapper.calls_after_failure == []
    else:
        assert state_counts(warehouse) == dict.fromkeys(STATE_TABLES, 0)
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 0
        assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 0
    warehouse.close()
    warehouse = warehouse_at(path)
    expected = 1 if stage == "commit_after" else 0
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == expected
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == expected
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM ops.ingestion_metadata WHERE status='succeeded'"
        )
        == expected
    )
    assert load(warehouse, repository, raw) == 1 - expected
    assert load(warehouse, repository, raw) == 0
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == 1
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM ops.ingestion_metadata WHERE status='succeeded'"
        )
        == 1
    )
    warehouse.close()


def test_loader_stops_after_unknown_commit_without_failed_receipt(tmp_path):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    repository.add("100", segb(event("app.first"))[0])
    repository.add("200", segb(event("app.second"))[0])
    # Inject only after the lease transaction has completed.
    original_load = warehouse.load_object

    def interrupted_load(*args, **kwargs):
        warehouse.connection = InterruptedConnection(warehouse.connection, "commit_after")
        return original_load(*args, **kwargs)

    warehouse.load_object = interrupted_load
    with pytest.raises(WarehouseConnectionError):
        run_loader(repository, warehouse)
    wrapper = warehouse.connection
    assert wrapper.calls_after_failure == []
    assert wrapper.connection.execute("SELECT status FROM ops.ingestion_metadata").fetchall() == [
        ("succeeded",)
    ]
    assert wrapper.connection.execute("SELECT count(*) FROM base.screen_time_event").fetchone() == (
        1,
    )
    warehouse.close()


def test_same_content_v1_to_v2_and_representative_fallback(tmp_path):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    segment = segb(event("app.copy"))[0]
    first = repository.add("100", segment, version=1)
    load(warehouse, repository, first)
    original = warehouse.query_rows("SELECT * FROM base.screen_time_event")
    load(warehouse, repository, repository.add("100", segment))
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == 1
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event") == original
    second = repository.add("200", segment)
    load(warehouse, repository, second)
    assert (
        warehouse.query_value("SELECT duplicate_occurrence_count FROM base.screen_time_event") == 1
    )
    empty = repository.add("200", b"")
    warehouse.load_object(empty, byte_size=0, batch=ScreenTimeBatch([], "200", "events"))
    assert warehouse.query_rows(
        "SELECT segment_key, duplicate_occurrence_count, is_active FROM base.screen_time_event"
    ) == [(first.logical_key, 0, True)]
    warehouse.close()


def test_rollback_restores_existing_events_matches_and_success(tmp_path):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    payload = event("app.history")
    segment, offset = segb(payload)
    load(warehouse, repository, repository.add("100", segment))
    load(warehouse, repository, repository.add("200", segment))
    tables = [f"ops.screen_time_{t}" for t in STATE_TABLES] + [
        "base.screen_time_event",
        "ops.ingestion_metadata",
    ]
    before = {t: warehouse.query_rows(f"SELECT * FROM {t} ORDER BY ALL") for t in tables}
    deletion = repository.add(
        "100", segb(tombstone("100", offset, len(payload)))[0], kind="tombstones"
    )
    connection = warehouse.connection
    warehouse.connection = InterruptedConnection(connection, "events")
    with pytest.raises(OSError):
        load(warehouse, repository, deletion)
    assert {t: warehouse.query_rows(f"SELECT * FROM {t} ORDER BY ALL") for t in tables} == before
    warehouse.connection = connection
    load(warehouse, repository, deletion)
    assert warehouse.query_rows("SELECT is_active FROM base.screen_time_event") == [(False,)]
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_deletion_match") == 1
    warehouse.close()


def test_migration_compacts_repeated_history_and_late_deletion(tmp_path):
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "events.duckdb"))))
    migrations = tmp_path / "legacy_migrations"
    migrations.mkdir()
    for path in DEFAULT_MIGRATIONS.glob("00[1-5]*.sql"):
        shutil.copyfile(path, migrations / path.name)
    warehouse.migrate(migrations)
    repository = Repository()
    payload = event("app.legacy")
    segment, offset = segb(payload)
    for i in range(10):
        raw = repository.add("100", segment, version=1 if i == 0 else 2)
        b = decode(repository, raw)
        warehouse.load_object(
            raw,
            byte_size=1,
            batch=LegacyScreenTimeBatch(b.records, b.source_segment_name, b.segment_kind),
        )
    legacy_rows = warehouse.query_rows(
        "SELECT * FROM base.screen_time_record_occurrence ORDER BY ALL"
    )
    warehouse.migrate()
    counts = state_counts(warehouse)
    assert counts == {"segment": 1, "record": 1, "tombstone": 0, "deletion_match": 0}
    warehouse.migrate()
    assert state_counts(warehouse) == counts
    assert (
        warehouse.query_rows("SELECT * FROM base.screen_time_record_occurrence ORDER BY ALL")
        == legacy_rows
    )
    load(warehouse, repository, repository.add("100", segment))
    assert state_counts(warehouse) == counts
    # Matching uses exactly the same identity after initialization and live decode.
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == 1
    deletion = repository.add(
        "100", segb(tombstone("100", offset, len(payload)))[0], kind="tombstones"
    )
    load(warehouse, repository, deletion)
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is False
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_deletion_match") == 1
    assert (
        warehouse.query_rows("SELECT * FROM base.screen_time_record_occurrence ORDER BY ALL")
        == legacy_rows
    )
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='ops' AND table_name='screen_time_checkpoint'"
        )
        == 0
    )
    warehouse.close()


@pytest.mark.parametrize("failure", ["begin", "rollback"])
def test_broken_connection_stops_every_followup_write(tmp_path, failure):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    raw = repository.add("100", segb(event("app.failure"))[0])

    class BrokenConnection(InterruptedConnection):
        def execute(self, sql, *args):
            if sql == ("BEGIN TRANSACTION" if failure == "begin" else "ROLLBACK"):
                raise OSError("connection closed")
            return super().execute(sql, *args)

    wrapper = BrokenConnection(warehouse.connection, "events")
    warehouse.connection = wrapper
    with pytest.raises(WarehouseConnectionError):
        load(warehouse, repository, raw)
    assert warehouse.connection_usable is False
    wrapper.calls_after_failure.clear()
    with pytest.raises(WarehouseConnectionError):
        load(warehouse, repository, raw)
    warehouse.release_job_lock("loader", "owner")
    assert wrapper.calls_after_failure == []
    warehouse.close()

from __future__ import annotations

import gzip
from dataclasses import replace
from datetime import timedelta

import pytest

from personal_data_platform.loader.job import run_loader
from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import (
    Warehouse,
    WarehouseConfig,
    WarehouseConnectionError,
    connect,
)
from tests.screen_time_helpers import Repository, event, segb


def decode(repository, raw):
    return ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))


def warehouse_at(path):
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    return warehouse


def test_large_screen_time_batch_uses_one_columnar_insert(tmp_path):
    repository = Repository()
    raw = repository.add("100", segb(event("batch.app"))[0])
    decoded = decode(repository, raw)
    prototype = decoded.records[0]
    batch = replace(
        decoded,
        records=[
            replace(
                prototype,
                event_key=f"{index:064x}",
                original_payload=f"record-{index}".encode(),
                record_offset=32 + 80 * index,
                record_metadata_offset=200000 + 16 * index,
                event_at=prototype.event_at + timedelta(seconds=index),
            )
            for index in range(2001)
        ],
    )
    warehouse = warehouse_at(tmp_path / "columnar.duckdb")

    class RemoteConnection:
        inserts = 0

        def execute(self, sql, *args):
            if "INSERT INTO screen_time_input" in sql:
                self.inserts += 1
            return warehouse_connection.execute(sql, *args)

        def executemany(self, sql, *args):
            raise AssertionError("Row-by-row remote inserts exceed the daily job budget")

        def __getattr__(self, name):
            return getattr(warehouse_connection, name)

    warehouse_connection = warehouse.connection
    remote = RemoteConnection()
    warehouse.connection = remote
    try:
        warehouse.load_object(raw, byte_size=1, batch=batch)
        assert remote.inserts == 1
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 2001
        assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_record") == 2001
        assert warehouse.query_value("SELECT record_count FROM ops.ingestion_metadata") == 2001
    finally:
        warehouse.close()


@pytest.mark.parametrize("version", [1, 2])
def test_unknown_segb_state_fails_without_replacing_last_good_observation(tmp_path, version):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    payload = event("app.retained")
    initial = repository.add("100", segb(payload)[0], version=version)
    assert run_loader(repository, warehouse).succeeded == 1
    assert warehouse.query_rows("SELECT is_active FROM base.screen_time_event") == [(True,)]
    tables = [f"ops.screen_time_{t}" for t in STATE_TABLES] + ["base.screen_time_event"]
    before = {t: warehouse.query_rows(f"SELECT * FROM {t} ORDER BY ALL") for t in tables}
    invalid = repository.add("100", segb(payload, state=99)[0], version=version)

    # Repeated attempts must remain failures and leave the last good snapshot intact.
    for _ in range(2):
        summary = run_loader(repository, warehouse)

        assert (summary.succeeded, summary.failed, summary.skipped, summary.records) == (0, 1, 1, 0)
        assert {
            t: warehouse.query_rows(f"SELECT * FROM {t} ORDER BY ALL") for t in tables
        } == before
        assert warehouse.query_rows(
            "SELECT status, error_type FROM ops.ingestion_metadata WHERE object_key = ?",
            [invalid.key],
        ) == [("failed", "SegmentDecodeError")]
        assert warehouse.query_rows(
            "SELECT status FROM ops.ingestion_metadata WHERE object_key = ?", [initial.key]
        ) == [("succeeded",)]
    warehouse.close()


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


STATE_TABLES = ("segment", "record")


def state_counts(warehouse):
    return {
        t: warehouse.query_value(f"SELECT count(*) FROM ops.screen_time_{t}") for t in STATE_TABLES
    }


def load(warehouse, repository, raw):
    count = warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))
    assert_full_resolution(warehouse)
    return count


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
            or (self.stage == "state" and "INSERT INTO ops.screen_time_record" in sql)
        )
        if match and not self.failed:
            self.failed = True
            if self.stage == "commit_after":
                self.connection.execute(sql, *args)
            raise OSError("database response lost")
        return self.connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize("stage", ["commit_before", "commit_after"])
def test_receipt_pruning_stops_after_uncertain_commit(tmp_path, stage):
    path = tmp_path / "receipts.duckdb"
    warehouse = warehouse_at(path)
    repository = Repository()
    old = repository.add("100", segb(event("app.old"))[0])
    current = repository.add("100", segb(event("app.current"))[0])
    for raw in (old, current):
        load(warehouse, repository, raw)
    wrapper = InterruptedConnection(warehouse.connection, stage)
    warehouse.connection = wrapper
    try:
        with pytest.raises(WarehouseConnectionError):
            warehouse.prune_screen_time_receipts(
                [current], parser_version=ScreenTimeSource().parser_version
            )
        assert warehouse.connection_usable is False
        warehouse.release_job_lock("loader", "owner")
        assert wrapper.calls_after_failure == []
    finally:
        warehouse.close()
    warehouse = warehouse_at(path)
    try:
        expected = (current.key,) if stage == "commit_after" else (old.key, current.key)
        assert warehouse.query_rows(
            "SELECT object_key FROM ops.ingestion_metadata ORDER BY object_key"
        ) == [(key,) for key in sorted(expected)]
    finally:
        warehouse.close()


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


def assert_full_resolution(warehouse):
    from personal_data_platform.sources.screen_time.event_state import ANALYTICAL_COLUMNS

    columns = ", ".join((*ANALYTICAL_COLUMNS, "is_active"))
    expected = warehouse.query_rows(
        f"SELECT {columns} FROM ops.screen_time_resolve("
        "(SELECT list(DISTINCT event_key) FROM ops.screen_time_record)) ORDER BY event_key"
    )
    actual = warehouse.query_rows(
        f"SELECT {columns} FROM base.screen_time_event "
        "WHERE event_key IN (SELECT event_key FROM ops.screen_time_record) ORDER BY event_key"
    )
    assert actual == expected
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM base.screen_time_event e WHERE is_active AND NOT EXISTS "
            "(SELECT 1 FROM ops.screen_time_record r WHERE r.event_key = e.event_key)"
        )
        == 0
    )


@pytest.mark.parametrize("history", [1, 30])
def test_segment_history_does_not_expand_resolve(tmp_path, history):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    for i in range(history):
        load(warehouse, repository, repository.add("100", segb(event(f"past.{i}"))[0]))
    raw = repository.add("100", segb(event("current"))[0])
    load(warehouse, repository, raw)
    assert warehouse.query_value("SELECT count(*) FROM screen_time_affected_event") == 2
    before = warehouse.query_rows("SELECT * FROM base.screen_time_event ORDER BY event_key")
    load(warehouse, repository, repository.add("100", segb(event("current"))[0]))
    assert warehouse.query_value("SELECT count(*) FROM screen_time_affected_event") == 1
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event ORDER BY event_key") == before
    assert_full_resolution(warehouse)
    # A previously unseen older Raw with identical bytes loses the physical UPSERT.
    older = replace(raw, key=raw.key + "-late", observed_at=raw.observed_at - timedelta(seconds=1))
    warehouse.load_object(older, byte_size=1, batch=decode(repository, raw))
    assert warehouse.query_value("SELECT count(*) FROM screen_time_affected_event") == 0
    assert_full_resolution(warehouse)
    empty = repository.add("100", b"")
    warehouse.load_object(empty, byte_size=0, batch=ScreenTimeBatch([], "100", "events"))
    assert warehouse.query_value("SELECT count(*) FROM screen_time_affected_event") == 1
    assert_full_resolution(warehouse)
    warehouse.close()

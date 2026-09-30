from dataclasses import replace
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.storage.motherduck import Warehouse


@pytest.fixture
def warehouse():
    value = Warehouse(duckdb.connect())
    value.migrate()
    yield value
    value.close()


def snapshot(value=10, *, hour=0, end_hour=24, version=1, origin="api"):
    from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window

    base = datetime(2026, 9, 1, tzinfo=UTC)
    window = Window("steps", base + timedelta(hours=hour), base + timedelta(hours=end_hour))
    rows = (
        ()
        if value is None
        else (
            Record(
                "steps",
                str(hour),
                window.start,
                window.start,
                window.start + timedelta(minutes=1),
                value,
            ),
        )
    )
    return Snapshot("self", window, base + timedelta(days=version), rows, origin=origin)


def apply(warehouse, data):
    from personal_data_platform.sources.fitbit.writer import FitbitBatch

    warehouse.connection.execute("BEGIN")
    try:
        FitbitBatch(data).write_snapshot(
            warehouse.connection, source_key=str(data.fetched_at), loaded_at=data.fetched_at
        )
        warehouse.connection.execute("COMMIT")
    except Exception:
        warehouse.connection.execute("ROLLBACK")
        raise


def test_replace_empty_and_a_b_a(warehouse):
    for number, version in [(10, 1), (20, 2), (10, 3)]:
        apply(warehouse, snapshot(number, version=version))
        assert warehouse.query_value("select sum(value) from base.fitbit_steps") == number
    apply(warehouse, snapshot(None, version=4))
    assert warehouse.query_value("select count(*) from base.fitbit_steps") == 0


def test_full_source_digest_changes_with_unknown_fields_but_not_fetch_time(warehouse):
    from personal_data_platform.sources.fitbit.writer import can_skip_snapshot

    first = replace(snapshot(10), source_payload=({"unknown": {"value": 1}},))
    apply(warehouse, first)
    later = replace(first, fetched_at=first.fetched_at + timedelta(hours=1))
    assert can_skip_snapshot(warehouse, later)
    changed = replace(later, source_payload=({"unknown": {"value": 2}},))
    assert not can_skip_snapshot(warehouse, changed)


def test_full_source_digest_requires_exact_coverage_and_no_unresolved_intent(warehouse):
    from personal_data_platform.sources.fitbit.writer import can_skip_snapshot

    first = snapshot(10)
    apply(warehouse, first)
    assert can_skip_snapshot(
        warehouse, replace(first, fetched_at=first.fetched_at + timedelta(hours=1))
    )
    warehouse.connection.execute(
        "INSERT INTO ops.fitbit_raw_intent VALUES (?,?,?,?,?,?,?,?)",
        [
            "receipt",
            0,
            "self",
            "steps",
            first.window.start,
            first.window.end,
            "raw/fitbit/v1/pending",
            first.fetched_at,
        ],
    )
    assert not can_skip_snapshot(warehouse, first)


def test_loaded_raw_clears_earlier_intents_in_same_transaction(warehouse):
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.raw import encode_snapshot
    from personal_data_platform.sources.fitbit.writer import FitbitBatch, can_skip_snapshot

    first = snapshot(10)
    receipt_key = "receipts/fitbit/v1/2026-09-27/" + "a" * 32 + ".json"
    old = replace(first, fetched_at=first.fetched_at - timedelta(hours=1))
    for value in (old, first):
        key, _ = encode_snapshot(value)
        warehouse.connection.execute(
            "INSERT INTO ops.fitbit_raw_intent VALUES (?,?,?,?,?,?,?,?)",
            [
                receipt_key,
                0,
                "self",
                "steps",
                value.window.start,
                value.window.end,
                key,
                value.fetched_at,
            ],
        )
    assert not can_skip_snapshot(warehouse, first)
    key, _ = encode_snapshot(first)
    raw = FitbitSource().parse_raw_key(
        key, storage_created_at=first.fetched_at, storage_generation=1
    )
    warehouse.load_object(raw, byte_size=len(first.to_bytes()), batch=FitbitBatch(first))
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    assert can_skip_snapshot(
        warehouse, replace(first, fetched_at=first.fetched_at + timedelta(hours=1))
    )
    warehouse.connection.execute("DELETE FROM ops.fitbit_raw_intent")
    middle = snapshot(20, hour=8, end_hour=16, version=2)
    apply(warehouse, middle)
    assert not can_skip_snapshot(warehouse, first)


def test_old_overlapping_acquisition_cannot_erase_newer_empty_range(warehouse):
    apply(warehouse, snapshot(None, hour=12, version=3))
    apply(warehouse, snapshot(5, hour=0, version=2))
    apply(warehouse, snapshot(99, hour=12, version=1))
    assert warehouse.query_rows("select record_id, value from base.fitbit_steps") == [("0", 5.0)]


def test_writer_does_not_commit_its_callers_transaction(warehouse):
    from personal_data_platform.sources.fitbit.writer import FitbitBatch

    data = snapshot()
    warehouse.connection.execute("BEGIN")
    FitbitBatch(data).write_snapshot(
        warehouse.connection, source_key="test", loaded_at=data.fetched_at
    )
    warehouse.connection.execute("ROLLBACK")
    assert warehouse.query_value("select count(*) from base.fitbit_steps") == 0


def test_newer_middle_range_preserves_both_sides_of_an_older_snapshot(warehouse):
    left = snapshot(10, hour=0, version=2)
    middle = snapshot(20, hour=8, end_hour=16, version=3)
    right = snapshot(30, hour=16, version=2)
    apply(warehouse, middle)
    apply(warehouse, replace(left, records=left.records + right.records))

    assert warehouse.query_rows(
        "SELECT record_id, value FROM base.fitbit_steps ORDER BY cursor_at"
    ) == [("0", 10.0), ("8", 20.0), ("16", 30.0)]
    assert warehouse.query_rows(
        "SELECT range_start, range_end FROM ops.fitbit_coverage ORDER BY range_start"
    ) == [
        (left.window.start, middle.window.start),
        (middle.window.start, middle.window.end),
        (middle.window.end, left.window.end),
    ]


def test_rollback_restores_records_deletions_and_coverage_after_replacement(warehouse):
    from personal_data_platform.sources.fitbit.writer import FitbitBatch

    original = snapshot(10)
    apply(warehouse, original)
    coverage = warehouse.query_rows("SELECT * FROM ops.fitbit_coverage")
    empty = snapshot(None, hour=8, end_hour=16, version=2)
    warehouse.connection.execute("BEGIN")
    FitbitBatch(empty).write_snapshot(
        warehouse.connection, source_key="empty", loaded_at=empty.fetched_at
    )
    deletion = snapshot(None, version=3)
    FitbitBatch(deletion).write_snapshot(
        warehouse.connection, source_key="deleted", loaded_at=deletion.fetched_at
    )
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_deleted_record") == 1
    warehouse.connection.execute("ROLLBACK")

    assert warehouse.query_rows("SELECT record_id, value FROM base.fitbit_steps") == [("0", 10.0)]
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_deleted_record") == 0
    assert warehouse.query_rows("SELECT * FROM ops.fitbit_coverage") == coverage


def sleep_snapshot(day, version, *, child="stage", origin="api"):
    from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window

    cursor = datetime(2026, 9, day, tzinfo=UTC)
    return Snapshot(
        "self",
        Window("sleep", cursor, cursor + timedelta(days=1)),
        datetime(2026, 10, version, tzinfo=UTC),
        (
            Record(
                "sleep",
                "session",
                cursor,
                cursor,
                cursor + timedelta(hours=1),
                45,
                source_date=cursor.date(),
            ),
            Record(
                "sleep-stage",
                child,
                cursor,
                cursor,
                cursor + timedelta(hours=1),
                3600,
                parent_id="session",
                category="light",
            ),
        ),
        origin=origin,
    )


@pytest.mark.parametrize("stale_child", ["old", "stale"])
def test_moving_sleep_session_replaces_its_children_across_date_ranges(warehouse, stale_child):
    apply(warehouse, sleep_snapshot(1, 1, child="old"))
    apply(warehouse, sleep_snapshot(2, 3, child="new"))
    assert warehouse.query_rows("select record_id from base.fitbit_sleep_stage") == [("new",)]
    apply(warehouse, sleep_snapshot(1, 2, child=stale_child))
    assert warehouse.query_rows("select record_id from base.fitbit_sleep_stage") == [("new",)]
    # Returning to an earlier date/content is a real update, not a cached no-op.
    apply(warehouse, sleep_snapshot(1, 4, child="old"))
    assert warehouse.query_rows("select record_id from base.fitbit_sleep_stage") == [("old",)]
    assert warehouse.query_value("select extract(day from cursor_at) from base.fitbit_sleep") == 1


def test_old_range_cannot_resurrect_a_moved_then_deleted_id(warehouse):
    apply(warehouse, sleep_snapshot(1, 1, child="old"))
    apply(warehouse, sleep_snapshot(2, 3, child="new"))
    apply(warehouse, replace(sleep_snapshot(2, 4), records=()))
    apply(warehouse, sleep_snapshot(1, 2, child="old"))
    assert warehouse.query_value("select count(*) from base.fitbit_sleep") == 0
    assert warehouse.query_value("select count(*) from base.fitbit_sleep_stage") == 0
    apply(warehouse, sleep_snapshot(1, 5, child="restored"))
    assert warehouse.query_rows("select record_id from base.fitbit_sleep_stage") == [("restored",)]


def test_changed_dense_day_uses_bounded_sql_statements(warehouse):
    from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window
    from personal_data_platform.sources.fitbit.writer import FitbitBatch

    at = datetime(2026, 9, 1, tzinfo=UTC)
    records = tuple(
        Record("heart-rate", str(i), at + timedelta(seconds=i), at + timedelta(seconds=i), value=60)
        for i in range(100)
    )
    data = Snapshot("self", Window("heart-rate", at, at + timedelta(days=1)), at, records)
    apply(warehouse, data)
    statements = []

    class Connection:
        def execute(self, sql, parameters):
            statements.append(sql)
            return warehouse.connection.execute(sql, parameters)

    updated = replace(
        data,
        fetched_at=at + timedelta(days=1),
        records=(replace(records[0], value=61), *records[1:]),
    )
    FitbitBatch(updated).write_snapshot(
        Connection(), source_key="updated", loaded_at=updated.fetched_at
    )
    assert warehouse.query_value("select max(value) from base.fitbit_heart_rate") == 61
    assert len(statements) < 20

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window
from personal_data_platform.sources.fitbit.writer import FitbitBatch
from personal_data_platform.storage.motherduck import Warehouse

AT = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def warehouse():
    value = Warehouse(duckdb.connect())
    value.migrate()
    yield value
    value.close()


def point(identity, minute, value=10):
    start = AT + timedelta(minutes=minute)
    return Record(
        "steps",
        identity,
        start,
        start,
        start + timedelta(minutes=1),
        value,
        offset_seconds=0,
        end_offset_seconds=0,
        source_date=start.date(),
    )


def snapshot(records, *, start=0, end=1440, version=1):
    return Snapshot(
        "self",
        Window("steps", AT + timedelta(minutes=start), AT + timedelta(minutes=end)),
        AT + timedelta(days=version),
        tuple(records),
    )


def apply(warehouse, data):
    warehouse.connection.execute("BEGIN")
    try:
        result = FitbitBatch(data).write_snapshot(
            warehouse.connection,
            source_key=str(data.fetched_at),
            loaded_at=data.fetched_at,
        )
        warehouse.connection.execute("COMMIT")
        return result
    except Exception:
        warehouse.connection.execute("ROLLBACK")
        raise


def test_full_partial_full_submits_only_changed_steps(warehouse):
    original = snapshot([point("a", 0), point("b", 1)])
    assert apply(warehouse, original).submitted_rows == 2
    changed = point("b", 1, 20)
    assert apply(warehouse, snapshot([changed], start=1, end=2, version=2)).submitted_rows == 1
    assert apply(warehouse, snapshot([point("a", 0), changed], version=3)).submitted_rows == 0
    assert warehouse.query_rows(
        "SELECT record_id,value,fetched_at FROM base.fitbit_activity_interval ORDER BY record_id"
    ) == [("a", 10.0, original.fetched_at), ("b", 20.0, AT + timedelta(days=2))]
    assert apply(warehouse, snapshot([point("a", 0, 99), changed], version=2)).submitted_rows == 0


def test_changed_full_scope_preserves_equal_row_metadata(warehouse):
    original = snapshot([point("a", 0), point("b", 1)])
    apply(warehouse, original)
    result = apply(warehouse, snapshot([point("a", 0), point("b", 1, 20)], version=2))
    assert result.submitted_rows == 1
    assert warehouse.query_rows(
        "SELECT fetched_at,loaded_at FROM base.fitbit_activity_interval WHERE record_id='a'"
    ) == [(original.fetched_at, original.fetched_at)]


def test_empty_partial_steps_only_deletes_inside_range(warehouse):
    apply(warehouse, snapshot([point("a", 0), point("b", 1), point("c", 2)]))
    result = apply(warehouse, snapshot([], start=1, end=2, version=2))
    assert result.submitted_rows == 0
    assert warehouse.query_rows(
        "SELECT record_id FROM base.fitbit_activity_interval ORDER BY record_id"
    ) == [("a",), ("c",)]
    assert warehouse.query_rows("SELECT record_id FROM ops.fitbit_deleted_record") == [("b",)]


def test_step_identity_move_invalidates_old_range_hash(warehouse):
    original = snapshot([point("a", 0)])
    apply(warehouse, original)
    moved = point("a", 1440)
    assert apply(warehouse, snapshot([moved], start=1440, end=1441, version=3)).submitted_rows == 1
    assert (
        apply(warehouse, replace(original, fetched_at=AT + timedelta(days=2))).submitted_rows == 0
    )
    assert (
        warehouse.query_value("SELECT start_at FROM base.fitbit_activity_interval") == moved.start
    )
    assert (
        apply(warehouse, replace(original, fetched_at=AT + timedelta(days=4))).submitted_rows == 1
    )
    assert warehouse.query_value("SELECT start_at FROM base.fitbit_activity_interval") == AT


def test_changed_step_offsets_are_submitted(warehouse):
    first = point("a", 0)
    apply(warehouse, snapshot([first]))
    changed = replace(first, offset_seconds=3600, end_offset_seconds=3600)
    assert apply(warehouse, snapshot([changed], version=2)).submitted_rows == 1
    assert warehouse.query_value("SELECT offset_seconds FROM base.fitbit_activity_interval") == 3600


def test_steps_rollback_restores_rows_deletions_and_coverage(warehouse):
    apply(warehouse, snapshot([point("a", 0), point("b", 1)]))
    before = {
        table: warehouse.query_rows("SELECT * FROM " + table)
        for table in [
            "base.fitbit_activity_interval",
            "ops.fitbit_coverage",
            "ops.fitbit_deleted_record",
        ]
    }
    data = snapshot([point("a", 0, 20)], version=2)
    warehouse.connection.execute("BEGIN")
    FitbitBatch(data).write_snapshot(
        warehouse.connection,
        source_key="rollback",
        loaded_at=data.fetched_at,
    )
    warehouse.connection.execute("ROLLBACK")
    for table, rows in before.items():
        assert warehouse.query_rows("SELECT * FROM " + table) == rows

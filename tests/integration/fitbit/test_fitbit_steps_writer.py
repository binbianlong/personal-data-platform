from dataclasses import replace
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window
from personal_data_platform.sources.fitbit.motherduck import FitbitBatch
from personal_data_platform.storage.motherduck import Warehouse

AT = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def warehouse():
    value = Warehouse(duckdb.connect())
    value.migrate()
    yield value
    value.close()


@pytest.fixture
def sql_trace(warehouse, monkeypatch):
    connection = warehouse.connection
    statements = []

    class Connection:
        def execute(self, sql, parameters=None):
            statements.append(sql)
            return connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(connection, name)

    monkeypatch.setattr(warehouse, "connection", Connection())
    return statements


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


def civil_snapshot(records, *, day=0, version=1):
    return Snapshot(
        "self",
        Window("steps", AT + timedelta(days=day), AT + timedelta(days=day + 1), time_basis="civil"),
        AT + timedelta(days=version),
        tuple(records),
    )


def local_point(identity, minute, value=10, offset=32400):
    start = AT + timedelta(minutes=minute)
    return Record(
        "steps",
        identity,
        start,
        start,
        start + timedelta(minutes=1),
        value,
        offset_seconds=offset,
        end_offset_seconds=offset,
        source_date=(start + timedelta(seconds=offset)).date(),
    )


def test_full_partial_full_submits_only_changed_steps(warehouse):
    original = snapshot([point("a", 0), point("b", 1)])
    assert apply(warehouse, original).submitted_rows == 2
    changed = point("b", 1, 20)
    assert apply(warehouse, snapshot([changed], start=1, end=2)).submitted_rows == 1
    assert apply(warehouse, snapshot([point("a", 0), changed])).outcome == "unchanged"
    assert warehouse.query_rows(
        "SELECT steps_interval_id,steps FROM base.steps_interval ORDER BY 1"
    ) == [("a", 10), ("b", 20)]


@pytest.mark.parametrize("civil", [False, True])
def test_empty_steps_deletes_only_actual_cursor_range(warehouse, civil):
    rows = [local_point("left", -541), local_point("inside", -540), local_point("right", -539)]
    apply(warehouse, snapshot(rows, start=-542, end=-538))
    data = civil_snapshot([]) if civil else snapshot([], start=-540, end=-539)
    assert apply(warehouse, data).outcome == "range_replaced"
    assert warehouse.query_rows("SELECT steps_interval_id FROM base.steps_interval ORDER BY 1") == (
        [("left",)] if civil else [("left",), ("right",)]
    )


def test_steps_id_move_and_offset_correction(warehouse):
    original = snapshot([point("a", 0)])
    apply(warehouse, original)
    moved = replace(point("a", 1440), offset_seconds=3600, end_offset_seconds=3600)
    assert apply(warehouse, snapshot([moved], start=1440, end=1441)).submitted_rows == 1
    assert warehouse.query_rows("SELECT start_at,offset_seconds FROM base.steps_interval") == [
        (moved.start, 3600)
    ]
    assert apply(warehouse, original).submitted_rows == 1
    assert warehouse.query_value("SELECT start_at FROM base.steps_interval") == AT


def test_civil_confirmation_and_date_move_with_tokyo_session(warehouse):
    warehouse.connection.execute("SET TimeZone='Asia/Tokyo'")
    row = local_point("a", 14 * 60 + 30)
    apply(warehouse, snapshot([row]))
    assert apply(warehouse, civil_snapshot([row])).outcome == "unchanged"
    moved = replace(
        row,
        offset_seconds=36000,
        end_offset_seconds=36000,
        source_date=(AT + timedelta(days=1)).date(),
    )
    assert apply(warehouse, civil_snapshot([moved], day=1)).submitted_rows == 1
    apply(warehouse, civil_snapshot([]))
    assert warehouse.query_rows("SELECT source_date FROM base.steps_interval") == [
        (moved.source_date,)
    ]


@pytest.mark.parametrize("civil", [False, True])
def test_steps_rollback_restores_deleted_and_updated_rows(warehouse, civil):
    apply(warehouse, snapshot([point("a", 0), point("b", 1)]))
    before = warehouse.query_rows("SELECT * FROM base.steps_interval ORDER BY 1")
    data = (civil_snapshot if civil else snapshot)([point("a", 0, 20)])
    warehouse.connection.execute("BEGIN")
    FitbitBatch(data).write_snapshot(
        warehouse.connection, source_key="rollback", loaded_at=data.fetched_at
    )
    warehouse.connection.execute("ROLLBACK")
    assert warehouse.query_rows("SELECT * FROM base.steps_interval ORDER BY 1") == before


def test_civil_empty_preserves_rows_without_a_provider_date(warehouse):
    unknown = replace(point("unknown", 0), source_date=None, offset_seconds=None)
    apply(warehouse, snapshot([unknown, point("known", 1)]))
    apply(warehouse, civil_snapshot([]))
    assert warehouse.query_rows(
        "SELECT steps_interval_id,source_date FROM base.steps_interval"
    ) == [("unknown", None)]

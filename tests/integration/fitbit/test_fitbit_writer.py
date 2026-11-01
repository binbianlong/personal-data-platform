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
    from personal_data_platform.sources.fitbit.motherduck import FitbitBatch

    warehouse.connection.execute("BEGIN")
    try:
        result = FitbitBatch(data).write_snapshot(
            warehouse.connection, source_key=str(data.fetched_at), loaded_at=data.fetched_at
        )
        warehouse.connection.execute("COMMIT")
        return result
    except Exception:
        warehouse.connection.execute("ROLLBACK")
        raise


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


def test_replace_empty_and_a_b_a(warehouse):
    for number in (10, 20, 10):
        result = apply(warehouse, snapshot(number))
        assert result.submitted_rows == 1
        assert warehouse.query_value("SELECT sum(steps) FROM base.steps_interval") == number
    assert apply(warehouse, snapshot(10)).outcome == "unchanged"
    result = apply(warehouse, snapshot(None))
    assert result.outcome == "range_replaced"
    assert warehouse.query_value("SELECT count(*) FROM base.steps_interval") == 0
    assert apply(warehouse, snapshot(None)).outcome == "unchanged"


def test_partial_empty_preserves_border_and_other_metric(warehouse):
    from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window

    at = datetime(2026, 9, 1, tzinfo=UTC)
    records = tuple(
        Record(
            "steps",
            str(i),
            at + timedelta(hours=i),
            at + timedelta(hours=i),
            at + timedelta(hours=i, minutes=1),
            i,
        )
        for i in (0, 8, 16)
    )
    apply(warehouse, Snapshot("self", Window("steps", at, at + timedelta(days=1)), at, records))
    azm = replace(records[1], kind="active-zone-minutes", category="unknown")
    apply(
        warehouse,
        Snapshot("self", Window("active-zone-minutes", at, at + timedelta(days=1)), at, (azm,)),
    )
    apply(warehouse, snapshot(None, hour=8, end_hour=16))
    assert warehouse.query_rows("SELECT steps_interval_id FROM base.steps_interval ORDER BY 1") == [
        ("0",),
        ("16",),
    ]
    assert warehouse.query_rows("SELECT category FROM base.active_zone_interval") == [("unknown",)]


def test_writer_does_not_commit_its_callers_transaction(warehouse):
    from personal_data_platform.sources.fitbit.motherduck import FitbitBatch

    apply(warehouse, snapshot(10))
    before = warehouse.query_rows("SELECT * FROM base.steps_interval")
    warehouse.connection.execute("BEGIN")
    FitbitBatch(snapshot(20)).write_snapshot(
        warehouse.connection, source_key="test", loaded_at=snapshot().fetched_at
    )
    assert warehouse.query_value("SELECT steps FROM base.steps_interval") == 20
    warehouse.connection.execute("ROLLBACK")
    assert warehouse.query_rows("SELECT * FROM base.steps_interval") == before


def test_sleep_move_kind_collision_and_empty(warehouse):
    from personal_data_platform.sources.fitbit.motherduck import sleep_detail_id

    old = sleep_snapshot(1, 1, child="shared")
    wake = replace(old.records[1], kind="sleep-wake", category="unknown-wake")
    apply(warehouse, replace(old, records=(*old.records, wake)))
    assert warehouse.query_rows(
        "SELECT sleep_detail_id,kind FROM base.sleep_detail ORDER BY kind"
    ) == [
        (sleep_detail_id("sleep-stage", "shared"), "sleep-stage"),
        (sleep_detail_id("sleep-wake", "shared"), "sleep-wake"),
    ]
    apply(warehouse, sleep_snapshot(2, 2, child="moved"))
    assert warehouse.query_rows(
        "SELECT sleep_detail_id,sleep_session_id FROM base.sleep_detail"
    ) == [(sleep_detail_id("sleep-stage", "moved"), "session")]
    assert warehouse.query_rows("SELECT sleep_session_id,source_date FROM base.sleep_session") == [
        ("session", datetime(2026, 9, 2).date())
    ]
    apply(warehouse, replace(sleep_snapshot(1, 3), records=()))
    assert warehouse.query_value("SELECT count(*) FROM base.sleep_session") == 1
    apply(warehouse, replace(sleep_snapshot(2, 4), records=()))
    assert warehouse.query_value("SELECT count(*) FROM base.sleep_session") == 0
    assert warehouse.query_value("SELECT count(*) FROM base.sleep_detail") == 0


def test_identical_sleep_reacquisition_has_no_mutations(warehouse):
    from personal_data_platform.sources.fitbit.motherduck import FitbitBatch

    data = sleep_snapshot(1, 1)
    apply(warehouse, data)
    statements = []

    class Connection:
        def execute(self, sql, parameters=None):
            statements.append(sql)
            return warehouse.connection.execute(sql, parameters)

    result = FitbitBatch(data).write_snapshot(
        Connection(), source_key="again", loaded_at=data.fetched_at
    )
    assert result.outcome == "unchanged" and result.submitted_rows == 0
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "DELETE", "UPDATE", "MERGE"))
        for sql in statements
    )


def test_changed_dense_day_uses_bounded_sql_statements(warehouse):
    from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window
    from personal_data_platform.sources.fitbit.motherduck import FitbitBatch

    at = datetime(2026, 9, 1, tzinfo=UTC)
    records = tuple(
        Record(
            "steps",
            str(i),
            at + timedelta(minutes=i),
            at + timedelta(minutes=i),
            at + timedelta(minutes=i + 1),
            60,
        )
        for i in range(100)
    )
    data = Snapshot("self", Window("steps", at, at + timedelta(days=1)), at, records)
    apply(warehouse, data)
    statements = []

    class Connection:
        def execute(self, sql, parameters=None):
            statements.append(sql)
            return warehouse.connection.execute(sql, parameters)

    result = FitbitBatch(
        replace(data, records=(replace(records[0], value=61), *records[1:]))
    ).write_snapshot(Connection(), source_key="new", loaded_at=at)
    assert result.submitted_rows == 1
    assert warehouse.query_value("SELECT max(steps) FROM base.steps_interval") == 61
    assert len(statements) < 10


def test_minute_actual_diff_range_and_identity(warehouse):
    from personal_data_platform.sources.fitbit.models import (
        GOOGLE_WEARABLES,
        HEART_RATE_AGGREGATION_VERSION,
        HeartRateMinute,
        HeartRateMinuteSnapshot,
        Window,
    )
    from personal_data_platform.sources.fitbit.motherduck import (
        FitbitMinuteBatch,
        heart_rate_minute_id,
    )

    at = datetime(2026, 9, 1, tzinfo=UTC)
    rows = tuple(
        HeartRateMinute(at + timedelta(minutes=i), at + timedelta(minutes=i + 1), 60, 50, 70)
        for i in range(65)
    )
    data = HeartRateMinuteSnapshot(
        "self", Window("heart-rate", at, at + timedelta(minutes=65)), at + timedelta(hours=2), rows
    )

    def write(value, connection=None):
        return FitbitMinuteBatch(value).write_snapshot(
            connection or warehouse.connection, source_key="unused", loaded_at=value.fetched_at
        )

    assert write(data).submitted_rows == 65
    assert write(replace(data, subject_key="another")).outcome == "unchanged"
    assert warehouse.query_value(
        "SELECT heart_rate_minute_id FROM base.heart_rate_minute ORDER BY start_at LIMIT 1"
    ) == heart_rate_minute_id(GOOGLE_WEARABLES, at, HEART_RATE_AGGREGATION_VERSION)
    changed = replace(data, minutes=(rows[0], replace(rows[1], average=65), *rows[3:]))
    assert write(changed).submitted_rows == 1
    assert warehouse.query_value("SELECT count(*) FROM base.heart_rate_minute") == 64
    assert write(data).submitted_rows == 2
    middle = replace(
        data,
        window=Window("heart-rate", at + timedelta(minutes=1), at + timedelta(minutes=2)),
        minutes=(),
    )
    assert write(middle).outcome == "range_replaced"
    assert warehouse.query_value("SELECT count(*) FROM base.heart_rate_minute") == 64
    warehouse.connection.execute("BEGIN")
    write(data)
    warehouse.connection.execute("ROLLBACK")
    assert warehouse.query_value("SELECT count(*) FROM base.heart_rate_minute") == 64


def test_minute_identity_uses_normalized_utc_and_family_version():
    from personal_data_platform.sources.fitbit.motherduck import heart_rate_minute_id

    at = datetime.fromisoformat("2026-09-01T09:00:00+09:00")
    assert heart_rate_minute_id("a", at, "v1") == heart_rate_minute_id(
        "a", at.astimezone(UTC), "v1"
    )
    assert (
        len(
            {
                heart_rate_minute_id("a", at, "v1"),
                heart_rate_minute_id("b", at, "v1"),
                heart_rate_minute_id("a", at, "v2"),
            }
        )
        == 3
    )


def test_sleep_failure_after_child_replacement_rolls_back_both_tables(warehouse):
    from personal_data_platform.sources.fitbit.motherduck import FitbitBatch

    apply(warehouse, sleep_snapshot(1, 1, child="old"))
    before = {
        table: warehouse.query_rows(f"SELECT * FROM base.{table} ORDER BY ALL")
        for table in ("sleep_session", "sleep_detail")
    }

    class FailingConnection:
        def execute(self, sql, parameters=None):
            if sql.startswith("INSERT INTO base.sleep_session"):
                assert warehouse.query_value("SELECT count(*) FROM base.sleep_detail") == 1
                raise duckdb.ConstraintException("parent insert failed")
            return warehouse.connection.execute(sql, parameters)

    data = sleep_snapshot(2, 2, child="new")
    warehouse.connection.execute("BEGIN")
    try:
        with pytest.raises(duckdb.ConstraintException, match="parent insert failed"):
            FitbitBatch(data).write_snapshot(
                FailingConnection(), source_key="unused", loaded_at=data.fetched_at
            )
    finally:
        warehouse.connection.execute("ROLLBACK")
    for table, rows in before.items():
        assert warehouse.query_rows(f"SELECT * FROM base.{table} ORDER BY ALL") == rows

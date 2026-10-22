import shutil
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit.models import (
    GOOGLE_WEARABLES,
    HeartRateMinute,
    HeartRateMinuteSnapshot,
    Record,
    Snapshot,
    Window,
)
from personal_data_platform.sources.fitbit.writer import FitbitMinuteBatch
from personal_data_platform.storage.motherduck import DEFAULT_MIGRATIONS, Warehouse
from tests.integration.fitbit.test_fitbit_writer import apply, sleep_snapshot, snapshot


@pytest.fixture
def warehouse():
    value = Warehouse(duckdb.connect())
    value.migrate()
    yield value
    value.close()


def test_fitbit_schema_has_five_typed_tables_and_one_coverage(warehouse):
    assert {
        row[0]
        for row in warehouse.query_rows(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='base' "
            "AND table_name LIKE 'fitbit_%'"
        )
    } == {
        "fitbit_activity_interval",
        "fitbit_resting_heart_rate_daily",
        "fitbit_sleep_session",
        "fitbit_sleep_detail",
        "fitbit_heart_rate_minute",
    }
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='ops' "
            "AND table_name LIKE 'fitbit_%coverage'"
        )
        == 1
    )
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema='base' "
            "AND table_name LIKE 'fitbit_%' AND column_name IN ('origin','cursor_at','parent_id')"
        )
        == 0
    )


def test_minute_column_removal_preserves_values_and_ordering_metadata(tmp_path):
    for path in DEFAULT_MIGRATIONS.glob("00[1234]_*.sql"):
        shutil.copyfile(path, tmp_path / path.name)
    warehouse = Warehouse(duckdb.connect())
    try:
        warehouse.migrate(tmp_path)
        at = datetime(2026, 9, 1, tzinfo=UTC)
        warehouse.connection.execute(
            "INSERT INTO base.fitbit_heart_rate_minute VALUES "
            "('self',?,?,'heart-rate-minute-v1',?,60,50,70,3,?,'api-id',?)",
            [GOOGLE_WEARABLES, at, at + timedelta(minutes=1), at, at],
        )
        before = warehouse.query_rows(
            "SELECT * EXCLUDE (end_at, sample_count) FROM base.fitbit_heart_rate_minute"
        )
        warehouse.connection.execute(
            "CREATE VIEW marts.fitbit_heart_rate_minute_time_series AS "
            "SELECT subject_key,data_source_family,start_at,end_at,average AS mean_minute_heart_rate,"
            "minimum AS min_heart_rate,maximum AS max_heart_rate,sample_count,aggregation_version,"
            "fetched_at,source_key FROM base.fitbit_heart_rate_minute"
        )

        warehouse.migrate()

        assert not {"end_at", "sample_count"} & {
            row[0] for row in warehouse.query_rows("DESCRIBE base.fitbit_heart_rate_minute")
        }
        assert warehouse.query_rows("SELECT * FROM base.fitbit_heart_rate_minute") == before
        assert warehouse.query_rows(
            "SELECT start_at,end_at FROM marts.fitbit_heart_rate_minute_time_series"
        ) == [(at, at + timedelta(minutes=1))]
        assert "sample_count" not in {
            row[0]
            for row in warehouse.query_rows("DESCRIBE marts.fitbit_heart_rate_minute_time_series")
        }
        warehouse.migrate()
        assert warehouse.query_rows("SELECT * FROM base.fitbit_heart_rate_minute") == before
    finally:
        warehouse.close()


def test_shared_activity_ids_are_isolated_by_metric(warehouse):
    steps = snapshot(10)
    azm = replace(
        steps,
        window=replace(steps.window, data_type="active-zone-minutes"),
        records=(replace(steps.records[0], kind="active-zone-minutes", value=2),),
    )
    apply(warehouse, steps)
    apply(warehouse, azm)
    apply(warehouse, snapshot(None, version=3))
    apply(warehouse, steps)
    assert warehouse.query_rows(
        "SELECT metric,record_id,value FROM base.fitbit_activity_interval"
    ) == [("active-zone-minutes", "0", 2.0)]
    assert warehouse.query_rows("SELECT data_type FROM ops.fitbit_deleted_record") == [("steps",)]
    apply(warehouse, snapshot(20, version=4))
    assert warehouse.query_rows(
        "SELECT metric,value FROM base.fitbit_activity_interval ORDER BY metric"
    ) == [("active-zone-minutes", 2.0), ("steps", 20.0)]


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Tokyo"])
def test_provider_date_replacement_is_independent_of_session_timezone(warehouse, timezone):
    warehouse.connection.execute("SET TimeZone = ?", [timezone])
    at = datetime(2026, 9, 1, tzinfo=UTC)
    data = Snapshot(
        "self",
        Window("daily-resting-heart-rate", at, at + timedelta(days=1)),
        at,
        (Record("daily-resting-heart-rate", "rest", at, at, value=60, source_date=at.date()),),
    )
    apply(warehouse, data)
    assert warehouse.query_rows(
        "SELECT source_date,beats_per_minute FROM base.fitbit_resting_heart_rate_daily"
    ) == [(date(2026, 9, 1), 60.0)]
    apply(warehouse, replace(data, records=(), fetched_at=at + timedelta(days=2)))
    apply(warehouse, data)
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_resting_heart_rate_daily") == 0
    sleeping = sleep_snapshot(1, 1)
    apply(warehouse, sleeping)
    apply(
        warehouse, replace(sleeping, records=(), fetched_at=sleeping.fetched_at + timedelta(days=1))
    )
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_sleep_session") == 0
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_sleep_detail") == 0


def test_sleep_details_keep_overlapping_kinds_and_api_summary_minutes(warehouse):
    data = sleep_snapshot(1, 1, child="shared")
    wake = replace(
        data.records[1],
        kind="sleep-wake",
        end=data.records[1].start + timedelta(seconds=30),
        value=30,
        category=None,
    )
    apply(warehouse, replace(data, records=(*data.records, wake)))
    assert warehouse.query_rows("SELECT sleep_minutes FROM base.fitbit_sleep_session") == [(45.0,)]
    assert warehouse.query_rows(
        "SELECT kind,record_id,sleep_id,source_date,epoch(end_at-start_at) "
        "FROM base.fitbit_sleep_detail ORDER BY kind"
    ) == [
        ("sleep-stage", "shared", "session", date(2026, 9, 1), 3600.0),
        ("sleep-wake", "shared", "session", date(2026, 9, 1), 30.0),
    ]
    apply(warehouse, sleep_snapshot(1, 2, child="shared"))
    assert warehouse.query_rows("SELECT kind FROM base.fitbit_sleep_detail") == [("sleep-stage",)]


def test_minutes_and_records_share_coverage_without_affecting_each_other(warehouse):
    steps = snapshot()
    at = steps.window.start
    minute = HeartRateMinute(at, at + timedelta(minutes=1), 60, 50, 70, GOOGLE_WEARABLES)
    data = HeartRateMinuteSnapshot(
        "self", Window("heart-rate", at, at + timedelta(days=1)), at + timedelta(days=1), (minute,)
    )
    apply(warehouse, steps)
    FitbitMinuteBatch(data).write_snapshot(warehouse.connection, source_key="minute", loaded_at=at)
    FitbitMinuteBatch(replace(data, minutes=(), fetched_at=at + timedelta(days=2))).write_snapshot(
        warehouse.connection, source_key="empty", loaded_at=at
    )
    FitbitMinuteBatch(data).write_snapshot(warehouse.connection, source_key="stale", loaded_at=at)
    assert warehouse.query_rows("SELECT data_type FROM ops.fitbit_coverage ORDER BY data_type") == [
        ("heart-rate",),
        ("steps",),
    ]
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_heart_rate_minute") == 0
    assert warehouse.query_value("SELECT value FROM base.fitbit_activity_interval") == 10

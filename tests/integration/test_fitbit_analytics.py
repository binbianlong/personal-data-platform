from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window, date_cursor
from personal_data_platform.sources.fitbit.writer import FitbitBatch
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import _raw, _record


def test_health_views_split_tokyo_days_preserve_missing_and_keep_devices_separate(
    tmp_path,
    monkeypatch,
    dbt_project,
):
    database = tmp_path / "health.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    base = datetime(2026, 9, 1, tzinfo=UTC)
    warehouse.migrate()
    at = base + timedelta(hours=14, minutes=59, seconds=30)
    sleep_start = base + timedelta(hours=21)
    cursor = date_cursor(date(2026, 9, 2))
    groups = [
        ("steps", (Record("steps", "steps", at, at, at + timedelta(minutes=1), 6),)),
        (
            "active-zone-minutes",
            (Record("active-zone-minutes", "azm", at, at, at + timedelta(minutes=1), 2),),
        ),
        (
            "sleep",
            (
                Record(
                    "sleep",
                    "s",
                    cursor,
                    sleep_start,
                    sleep_start + timedelta(hours=1),
                    45,
                    source_date=cursor.date(),
                ),
                Record(
                    "sleep-stage",
                    "st",
                    cursor,
                    sleep_start,
                    sleep_start + timedelta(hours=1),
                    3600,
                    parent_id="s",
                    category="light",
                ),
                Record(
                    "sleep-wake",
                    "w",
                    cursor,
                    sleep_start,
                    sleep_start + timedelta(seconds=30),
                    30,
                    parent_id="s",
                ),
            ),
        ),
    ]
    for kind, records in groups:
        snapshot = Snapshot("self", Window(kind, base, base + timedelta(days=2)), base, records)
        FitbitBatch(snapshot).write_snapshot(
            warehouse.connection, source_key="test", loaded_at=base
        )
    for device, stream, parser in [
        ("phone", "app-in-focus", "app-in-focus-v1"),
        ("mac", "app-usage", "app-usage-v1"),
    ]:
        raw = replace(_raw(), key=device, subject_key=device, stream=stream)
        start = replace(
            _record(raw),
            event_key=device + "start",
            event_at=sleep_start - timedelta(minutes=90),
            parser_version=parser,
        )
        end = replace(
            start,
            event_key=device + "end",
            event_at=sleep_start - timedelta(minutes=60),
            in_foreground=False,
            record_offset=100,
            record_metadata_offset=200,
        )
        warehouse.load_object(raw, byte_size=1, batch=ScreenTimeBatch([start, end]))
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project, selector="tag:fitbit tag:screen_time")
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_rows(
            "select activity_date,steps,active_zone_minutes from marts.daily_fitbit_health order by activity_date"
        ) == [(date(2026, 9, 1), 3.0, 1.0), (date(2026, 9, 2), 3.0, 1.0)]
        assert (
            warehouse.query_value(
                "select resting_heart_rate from marts.daily_fitbit_health limit 1"
            )
            is None
        )
        assert (
            warehouse.query_value(
                "select sleep_minutes from marts.daily_fitbit_health where activity_date=?",
                [date(2026, 9, 2)],
            )
            == 45
        )
        assert warehouse.query_rows(
            "select device_key,platform,screen_time_seconds from marts.fitbit_sleep_screen_time order by device_key"
        ) == [("mac", "macos", 1800.0), ("phone", "ios", 1800.0)]
    finally:
        warehouse.close()


def test_minute_daily_average_has_observed_minute_semantics(
    tmp_path,
    monkeypatch,
    dbt_project,
):
    from personal_data_platform.sources.fitbit.models import (
        HeartRateMinute,
        HeartRateMinuteSnapshot,
    )
    from personal_data_platform.sources.fitbit.writer import FitbitMinuteBatch

    database = tmp_path / "minutes.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    warehouse.migrate()
    base = datetime(2026, 9, 1, 14, 59, tzinfo=UTC)
    minutes = tuple(
        HeartRateMinute(
            at,
            at + timedelta(minutes=1),
            avg,
            avg - 5,
            avg + 5,
            "users/me/dataSourceFamilies/google-wearables",
            sample_count=count,
        )
        for at, avg, count in [
            (base, 60, 1),
            (base + timedelta(minutes=1), 100, 50),
            (base + timedelta(minutes=2), 60, 10),
        ]
    )
    snapshot = HeartRateMinuteSnapshot(
        "self",
        Window("heart-rate", base, base + timedelta(minutes=3)),
        base + timedelta(days=1),
        minutes,
        (),
    )
    FitbitMinuteBatch(snapshot).write_snapshot(
        warehouse.connection, source_key="minutes", loaded_at=base + timedelta(days=1)
    )
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project, selector="tag:fitbit tag:screen_time")
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_rows(
            "SELECT activity_date,mean_minute_heart_rate,observed_heart_rate_minutes,min_heart_rate,max_heart_rate FROM marts.daily_fitbit_heart_rate_minute ORDER BY activity_date"
        ) == [(date(2026, 9, 1), 60, 1, 55, 65), (date(2026, 9, 2), 80, 2, 55, 105)]
        assert (
            warehouse.query_value(
                "SELECT mean_minute_heart_rate FROM marts.daily_fitbit_health WHERE activity_date=?",
                [date(2026, 9, 2)],
            )
            == 80
        )
    finally:
        warehouse.close()


def test_minute_writer_empty_deletes_and_stale_snapshot_protects_newer_subrange(tmp_path):
    from personal_data_platform.sources.fitbit.models import (
        HeartRateMinute,
        HeartRateMinuteSnapshot,
    )
    from personal_data_platform.sources.fitbit.writer import FitbitMinuteBatch

    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "minute-writer.duckdb"))))
    warehouse.migrate()
    base = datetime(2026, 9, 1, tzinfo=UTC)
    window = Window("heart-rate", base, base + timedelta(minutes=3))

    def snapshot(at, rows, scope=window):
        return HeartRateMinuteSnapshot("self", scope, at, tuple(rows), ())

    def minute(at, avg=60):
        return HeartRateMinute(
            at,
            at + timedelta(minutes=1),
            avg,
            avg,
            avg,
            "users/me/dataSourceFamilies/google-wearables",
        )

    try:
        FitbitMinuteBatch(
            snapshot(base + timedelta(hours=1), [minute(base), minute(base + timedelta(minutes=1))])
        ).write_snapshot(warehouse.connection, source_key="first", loaded_at=base)
        narrow = Window("heart-rate", base + timedelta(minutes=1), base + timedelta(minutes=2))
        FitbitMinuteBatch(
            snapshot(base + timedelta(hours=3), [minute(narrow.start, 100)], narrow)
        ).write_snapshot(warehouse.connection, source_key="new", loaded_at=base)
        FitbitMinuteBatch(snapshot(base + timedelta(hours=2), [])).write_snapshot(
            warehouse.connection, source_key="stale", loaded_at=base
        )
        assert warehouse.query_rows(
            "SELECT start_at,average,sample_count FROM base.fitbit_heart_rate_minute"
        ) == [(narrow.start, 100, None)]
        FitbitMinuteBatch(snapshot(base + timedelta(hours=4), [])).write_snapshot(
            warehouse.connection, source_key="empty", loaded_at=base
        )
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_heart_rate_minute") == 0
    finally:
        warehouse.close()


def test_minute_writer_keeps_existing_rows_when_a_later_api_page_fails(tmp_path):
    import pytest

    from personal_data_platform.sources.fitbit.api import TransientError
    from personal_data_platform.sources.fitbit.models import (
        HeartRateMinute,
        HeartRateMinuteSnapshot,
    )
    from personal_data_platform.sources.fitbit.writer import FitbitMinuteBatch
    from tests.unit.test_fitbit_api import FakeTransport, client, rollup

    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "partial.duckdb"))))
    warehouse.migrate()
    base = datetime(2026, 9, 1, tzinfo=UTC)
    window = Window("heart-rate", base, base + timedelta(days=1))
    snapshot = HeartRateMinuteSnapshot(
        "self",
        window,
        base + timedelta(days=1),
        (HeartRateMinute(base, base + timedelta(minutes=1), 60, 50, 70),),
        (),
    )
    try:
        FitbitMinuteBatch(snapshot).write_snapshot(
            warehouse.connection, source_key="existing", loaded_at=base
        )
        with pytest.raises(TransientError):
            acquired = client(
                FakeTransport(
                    [
                        {
                            "rollupDataPoints": [rollup(average=100, maximum=105)],
                            "nextPageToken": "next",
                        },
                        (503, {}, {}),
                    ]
                ),
                clock=lambda: base + timedelta(days=2),
            ).fetch_heart_rate_minutes(window, subject_key="self")
            FitbitMinuteBatch(acquired).write_snapshot(
                warehouse.connection, source_key="partial", loaded_at=base
            )
        assert warehouse.query_rows(
            "SELECT average,source_key FROM base.fitbit_heart_rate_minute"
        ) == [(60, "existing")]
    finally:
        warehouse.close()

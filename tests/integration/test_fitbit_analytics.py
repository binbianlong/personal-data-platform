from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window, date_cursor
from personal_data_platform.sources.fitbit.writer import FitbitBatch
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.integration.test_dbt_models import dbt_project  # noqa: F401
from tests.screen_time_helpers import _raw, _record


def test_health_views_split_tokyo_days_preserve_missing_and_keep_devices_separate(
    tmp_path,
    monkeypatch,
    dbt_project,  # noqa: F811
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
        ("heart-rate", (Record("heart-rate", "hr", base, base, value=65),)),
        (
            "sleep",
            (
                Record(
                    "sleep",
                    "s",
                    cursor,
                    sleep_start,
                    sleep_start + timedelta(hours=1),
                    60,
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
            == 60
        )
        assert warehouse.query_rows(
            "select device_key,platform,screen_time_seconds from marts.fitbit_sleep_screen_time order by device_key"
        ) == [("mac", "macos", 1800.0), ("phone", "ios", 1800.0)]
    finally:
        warehouse.close()

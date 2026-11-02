from datetime import UTC, datetime, timedelta

from personal_data_platform.sources.fitbit.models import Window
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


def test_minute_writer_empty_deletes_only_reacquired_range(tmp_path):
    from personal_data_platform.sources.fitbit.models import (
        HeartRateMinute,
        HeartRateMinuteSnapshot,
    )
    from personal_data_platform.sources.fitbit.motherduck import FitbitMinuteBatch

    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "minute-writer.duckdb"))))
    warehouse.migrate()
    base = datetime(2026, 9, 1, tzinfo=UTC)
    window = Window("heart-rate", base, base + timedelta(minutes=3))

    def snapshot(at, rows, scope=window):
        return HeartRateMinuteSnapshot("self", scope, at, tuple(rows))

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
        FitbitMinuteBatch(snapshot(base + timedelta(hours=4), [], narrow)).write_snapshot(
            warehouse.connection, source_key="empty-narrow", loaded_at=base
        )
        assert warehouse.query_rows("SELECT start_at,mean_bpm FROM base.heart_rate_minute") == [
            (base, 60)
        ]
        FitbitMinuteBatch(snapshot(base + timedelta(hours=4), [])).write_snapshot(
            warehouse.connection, source_key="empty", loaded_at=base
        )
        assert warehouse.query_value("SELECT count(*) FROM base.heart_rate_minute") == 0
    finally:
        warehouse.close()


def test_minute_writer_keeps_existing_rows_when_a_later_api_page_fails(tmp_path):
    import pytest

    from personal_data_platform.sources.fitbit.api import TransientError
    from personal_data_platform.sources.fitbit.models import (
        HeartRateMinute,
        HeartRateMinuteSnapshot,
    )
    from personal_data_platform.sources.fitbit.motherduck import FitbitMinuteBatch
    from tests.unit.fitbit.test_fitbit_api import FakeTransport, client, rollup

    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "partial.duckdb"))))
    warehouse.migrate()
    base = datetime(2026, 9, 1, tzinfo=UTC)
    window = Window("heart-rate", base, base + timedelta(days=1))
    snapshot = HeartRateMinuteSnapshot(
        "self",
        window,
        base + timedelta(days=1),
        (HeartRateMinute(base, base + timedelta(minutes=1), 60, 50, 70),),
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
        assert warehouse.query_rows("SELECT mean_bpm FROM base.heart_rate_minute") == [(60,)]
    finally:
        warehouse.close()

from datetime import UTC, datetime, timedelta

import pytest


def test_scalar_records_reject_second_level_heart_rate():
    from personal_data_platform.sources.fitbit.models import Record

    start = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="supported kind"):
        Record("heart-rate", "sample", start, start, value=60)


def test_snapshot_rejects_incomplete_acquisition():
    from personal_data_platform.sources.fitbit.models import Snapshot, Window

    start = datetime(2026, 9, 1, tzinfo=UTC)
    window = Window("steps", start, start + timedelta(days=1))
    with pytest.raises(ValueError, match="complete"):
        Snapshot("owner", window, start, (), complete=False)


def test_rejects_outside_records_and_naive_time():
    from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window

    start = datetime(2026, 9, 1, tzinfo=UTC)
    window = Window("steps", start, start + timedelta(days=1))
    with pytest.raises(ValueError, match="range"):
        Snapshot(
            "owner",
            window,
            start,
            (Record("steps", "a", window.end, window.end, window.end + timedelta(minutes=1), 1),),
        )
    with pytest.raises(ValueError, match="aware"):
        Window("steps", start.replace(tzinfo=None), start)


def test_civil_steps_validate_provider_date_instead_of_physical_cursor():
    from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window

    at = datetime(2026, 9, 1, tzinfo=UTC)
    start = at - timedelta(hours=9)
    record = Record(
        "steps",
        "a",
        start,
        start,
        start + timedelta(minutes=1),
        1,
        offset_seconds=32400,
        end_offset_seconds=32400,
        source_date=at.date(),
    )
    window = Window("steps", at, at + timedelta(days=1), time_basis="civil")
    assert Snapshot("self", window, at, (record,)).records == (record,)
    with pytest.raises(ValueError, match="range"):
        Snapshot(
            "self",
            Window("steps", at + timedelta(days=1), at + timedelta(days=2), time_basis="civil"),
            at,
            (record,),
        )

from datetime import UTC, datetime, timedelta

import pytest


def test_snapshot_roundtrip_and_incomplete_rejected():
    from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window

    start = datetime(2026, 9, 1, tzinfo=UTC)
    window = Window("steps", start, start + timedelta(days=1))
    snapshot = Snapshot(
        "owner",
        window,
        start,
        (Record("steps", "a", start, start, start + timedelta(minutes=1), 12),),
    )
    assert Snapshot.from_bytes(snapshot.to_bytes()) == snapshot
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

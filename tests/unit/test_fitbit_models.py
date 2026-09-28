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


def test_source_digest_ignores_api_page_order_but_keeps_original_raw_order():
    from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window

    start = datetime(2026, 9, 1, tzinfo=UTC)
    window = Window("steps", start, start + timedelta(days=1))
    first = Record("steps", "a", start, start, start + timedelta(minutes=1), 1)
    second_at = start + timedelta(minutes=1)
    second = Record("steps", "b", second_at, second_at, second_at + timedelta(minutes=1), 2)
    a = Snapshot(
        "owner", window, start, (first, second),
        source_payload=({"id": "a", "unknown": 1}, {"id": "b", "unknown": 2}),
    )
    b = Snapshot(
        "owner", window, start + timedelta(hours=1), (second, first),
        source_payload=({"id": "b", "unknown": 2}, {"id": "a", "unknown": 1}),
    )
    assert a.to_bytes() != b.to_bytes()
    assert a.source_sha256() == b.source_sha256()

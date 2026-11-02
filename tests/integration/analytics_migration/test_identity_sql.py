import duckdb
import pytest


@pytest.mark.parametrize("record", ["ascii", '記録 "一"', "back\\slash\nnewline"])
def test_sleep_detail_identity_matches_duckdb_utf8_json(record):
    from personal_data_platform.sources.fitbit.motherduck import sleep_detail_id

    connection = duckdb.connect()
    kind = "sleep-stage"
    actual = connection.execute("SELECT sha256(to_json([?, ?]))", [kind, record]).fetchone()[0]
    assert sleep_detail_id(kind, record) == actual
    connection.close()


def test_sleep_detail_ascii_identity_is_unchanged():
    import hashlib
    import json

    from personal_data_platform.sources.fitbit.motherduck import sleep_detail_id

    old = hashlib.sha256(
        json.dumps(["sleep-stage", "abc"], separators=(",", ":")).encode()
    ).hexdigest()
    assert sleep_detail_id("sleep-stage", "abc") == old


def test_heart_id_sql_matches_python_across_timezone_and_microseconds():
    from datetime import datetime, timedelta, timezone

    from personal_data_platform.sources.fitbit.motherduck import heart_rate_minute_id

    start = datetime(2026, 1, 1, 9, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9)))
    family, version = "users/me/dataSourceFamilies/google-wearables", "heart-rate-minute-v1"
    connection = duckdb.connect()
    sql = "SELECT sha256(to_json([?, strftime(?::TIMESTAMPTZ AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%f') || '+00:00', ?]))"
    assert connection.execute(sql, [family, start, version]).fetchone()[0] == heart_rate_minute_id(
        family, start, version
    )
    connection.close()

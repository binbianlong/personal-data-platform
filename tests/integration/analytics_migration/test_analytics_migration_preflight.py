from pathlib import Path

import duckdb
import pytest

from scripts.analytics_migration_preflight import LEGACY_CHECKSUMS, validate_legacy


@pytest.fixture
def legacy():
    connection = duckdb.connect()
    sql = Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text()
    connection.execute(sql)
    connection.executemany(
        "INSERT INTO ops.schema_migration VALUES (?, ?, current_timestamp)",
        list(LEGACY_CHECKSUMS.items()),
    )
    yield connection
    connection.close()


def test_legacy_empty_fixture_passes_preflight(legacy):
    validate_legacy(legacy, subject="account")


def test_checksum_mismatch_fails_closed(legacy):
    legacy.execute(
        "UPDATE ops.schema_migration SET checksum='wrong' WHERE migration_id='001_initial.sql'"
    )
    with pytest.raises(ValueError, match="history"):
        validate_legacy(legacy, subject="account")


def test_extra_column_fails_closed(legacy):
    legacy.execute("ALTER TABLE base.fitbit_exercise ADD COLUMN unsupported VARCHAR")
    with pytest.raises(ValueError, match="schema"):
        validate_legacy(legacy, subject="account")


def test_second_account_in_ops_fails_closed(legacy):
    legacy.execute(
        "INSERT INTO ops.fitbit_deleted_record VALUES ('other', 'steps', 'x', current_timestamp, 'source')"
    )
    with pytest.raises(ValueError, match="account"):
        validate_legacy(legacy, subject="account")


def test_daily_duplicates_fail_closed(legacy):
    legacy.execute(
        "INSERT INTO base.fitbit_resting_heart_rate_daily (subject_key, record_id, source_date, beats_per_minute, fetched_at, source_key, loaded_at) VALUES ('account', 'one', '2026-01-01', 60, current_timestamp, 'x', current_timestamp), ('account', 'two', '2026-01-01', 61, current_timestamp, 'x', current_timestamp)"
    )
    with pytest.raises(ValueError, match="duplicate daily"):
        validate_legacy(legacy, subject="account")


def test_orphan_sleep_detail_fails_closed(legacy):
    legacy.execute(
        "INSERT INTO base.fitbit_sleep_detail (subject_key, kind, record_id, sleep_id, source_date, start_at, end_at, category, fetched_at, source_key, loaded_at) VALUES ('account', 'sleep-stage', 'child', 'missing', '2026-01-01', current_timestamp, current_timestamp + interval '1 minute', 'unknown', current_timestamp, 'x', current_timestamp)"
    )
    with pytest.raises(ValueError, match="sleep detail"):
        validate_legacy(legacy, subject="account")


def test_unexpired_writer_lease_fails_closed(legacy):
    legacy.execute(
        "INSERT INTO ops.job_lock VALUES ('loader', 'owner', current_timestamp + INTERVAL '1 hour')"
    )
    with pytest.raises(ValueError, match="lease"):
        validate_legacy(legacy, subject="account")


def test_changed_key_constraints_fail_closed(legacy):
    sql = legacy.execute(
        "SELECT sql FROM duckdb_tables() WHERE schema_name='base' AND table_name='fitbit_resting_heart_rate_daily'"
    ).fetchone()[0]
    legacy.execute("DROP TABLE base.fitbit_resting_heart_rate_daily")
    sql = sql.rstrip(";").removesuffix(")") + ", UNIQUE(source_date))"
    legacy.execute(sql)
    with pytest.raises(ValueError, match="schema"):
        validate_legacy(legacy, subject="account")


def test_sleep_detail_same_provider_id_in_distinct_kinds_is_valid(legacy):
    legacy.execute(
        "INSERT INTO base.fitbit_sleep_session (subject_key,record_id,source_date,start_at,end_at,fetched_at,source_key,loaded_at) VALUES ('account','parent','2026-01-01', current_timestamp, current_timestamp + interval '1 minute', current_timestamp,'x',current_timestamp)"
    )
    for kind in ("sleep-stage", "sleep-wake"):
        legacy.execute(
            "INSERT INTO base.fitbit_sleep_detail (subject_key,kind,record_id,sleep_id,source_date,start_at,end_at,fetched_at,source_key,loaded_at) VALUES ('account',?,'same','parent','2026-01-01',current_timestamp,current_timestamp + interval '1 minute',current_timestamp,'x',current_timestamp)",
            [kind],
        )
    validate_legacy(legacy, subject="account")


def test_changed_check_constraint_fails_closed(legacy):
    sql = legacy.execute(
        "SELECT sql FROM duckdb_tables() WHERE schema_name='base' AND table_name='fitbit_resting_heart_rate_daily'"
    ).fetchone()[0]
    legacy.execute("DROP TABLE base.fitbit_resting_heart_rate_daily")
    sql = sql.replace(
        "CHECK((isfinite(beats_per_minute) AND (beats_per_minute >= 0)))",
        "CHECK((beats_per_minute > -1))",
    )
    legacy.execute(sql)
    with pytest.raises(ValueError, match="schema"):
        validate_legacy(legacy, subject="account")

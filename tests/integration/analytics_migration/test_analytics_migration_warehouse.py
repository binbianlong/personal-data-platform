from pathlib import Path

import duckdb
import pytest

from scripts.analytics_migration_preflight import LEGACY_CHECKSUMS
from scripts.migrate_analytics_schema import migrate_warehouse


def legacy_file(path: Path) -> None:
    connection = duckdb.connect(str(path))
    connection.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    connection.executemany(
        "INSERT INTO ops.schema_migration VALUES (?, ?, current_timestamp)",
        list(LEGACY_CHECKSUMS.items()),
    )
    connection.close()


def test_dry_run_does_not_create_target(tmp_path):
    source, target = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    legacy_file(source)
    migrate_warehouse(source, target, subject="account", apply=False)
    assert not target.exists()


def test_occupied_target_is_rejected(tmp_path):
    source, target = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    legacy_file(source)
    target.write_bytes(b"untouched")
    with pytest.raises(ValueError, match="target"):
        migrate_warehouse(source, target, subject="account", apply=True)
    assert target.read_bytes() == b"untouched"


def test_stage_has_baseline_and_all_seven_views(tmp_path):
    source, target = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    legacy_file(source)
    migrate_warehouse(source, target, subject="account", apply=True)
    connection = duckdb.connect(str(target), read_only=True)
    assert connection.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='base' AND table_type='BASE TABLE'"
    ).fetchone() == (18,)
    assert connection.execute(
        "SELECT count(*) FROM information_schema.views WHERE table_schema='marts'"
    ).fetchone() == (7,)
    assert connection.execute("SELECT count(*) FROM ops.schema_migration").fetchone() == (1,)
    connection.close()


def test_catalog_inventory_rejects_even_unrelated_view():
    from scripts.migrate_analytics_schema import assert_empty_catalog

    connection = duckdb.connect()
    connection.execute("CREATE VIEW unexpected AS SELECT 1")
    with pytest.raises(ValueError, match="target"):
        assert_empty_catalog(
            connection, connection.execute("SELECT current_database()").fetchone()[0]
        )
    connection.close()


def test_full_legacy_inventory_projects_values_and_writer_ids(tmp_path):
    from datetime import UTC, datetime

    from personal_data_platform.sources.fitbit.motherduck import (
        heart_rate_minute_id,
        sleep_detail_id,
    )

    source, target = tmp_path / "old.duckdb", tmp_path / "stage.duckdb"
    legacy_file(source)
    connection = duckdb.connect(str(source))
    table_names = [
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='base' ORDER BY table_name"
        ).fetchall()
    ]
    for table in table_names:
        columns = connection.execute(
            "SELECT column_name,data_type,is_nullable FROM information_schema.columns WHERE table_schema='base' AND table_name=? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        row = {}
        for column, typ, nullable in columns:
            values = {
                "subject_key": "account",
                "record_id": "id",
                "sleep_id": "id",
                "metric": "steps",
                "kind": "sleep-stage" if table == "fitbit_sleep_detail" else 1,
                "data_source_family": "users/me/dataSourceFamilies/google-wearables",
                "aggregation_version": "heart-rate-minute-v1",
                "platform": "ios" if table.startswith("iphone") else "macos",
                "source_stream": "app-in-focus" if table.startswith("iphone") else "app-usage",
                "state": "start",
                "is_active": False,
                "motionactivities": ["unknown", None],
                "event_key": table,
                "device_key": "device",
                "segment_key": "segment",
                "object_key": "object",
                "category": "unknown",
                "exercise_type": "unknown",
                "zone": "unknown",
                "is_main_sleep": None,
            }
            if column in values:
                value = values[column]
            elif column == "end_at":
                value = "2026-01-01 00:01:00.123456+00"
            elif typ == "TIMESTAMP WITH TIME ZONE":
                value = (
                    "2026-01-01 00:00:00+00"
                    if table == "fitbit_heart_rate_minute"
                    else "2026-01-01 00:00:00.123456+00"
                )
            elif typ == "DATE":
                value = "2025-12-31"
            elif typ in ("DOUBLE", "BIGINT", "INTEGER", "UINTEGER"):
                value = 12.5 if typ == "DOUBLE" else 1
            elif typ == "BOOLEAN":
                value = False
            elif nullable == "YES":
                value = None
            else:
                value = "value"
            row[column] = value
        names = ",".join('"' + name + '"' for name in row)
        connection.execute(
            f'INSERT INTO base."{table}" ({names}) VALUES ({",".join("?" for _ in row)})',
            list(row.values()),
        )
    connection.execute(
        "INSERT INTO base.fitbit_activity_interval SELECT subject_key,'active-zone-minutes',record_id,start_at,end_at,\"value\",offset_seconds,end_offset_seconds,source_date,'unknown',fetched_at,source_key,loaded_at FROM base.fitbit_activity_interval"
    )
    connection.close()
    migrate_warehouse(source, target, subject="account", apply=True)
    result = duckdb.connect(str(target), read_only=True)
    counts = dict(
        result.execute(
            "SELECT table_name, estimated_size FROM duckdb_tables() WHERE schema_name='base'"
        ).fetchall()
    )
    assert len(counts) == 18
    assert all(counts[t] == 1 for t in counts if t not in {"app", "profile", "place"})
    assert all(counts[t] == 0 for t in {"app", "profile", "place"})
    assert result.execute("SELECT sleep_detail_id,category FROM base.sleep_detail").fetchone() == (
        sleep_detail_id("sleep-stage", "id"),
        "unknown",
    )
    assert result.execute("SELECT heart_rate_minute_id FROM base.heart_rate_minute").fetchone() == (
        heart_rate_minute_id(
            "users/me/dataSourceFamilies/google-wearables",
            datetime(2026, 1, 1, tzinfo=UTC),
            "heart-rate-minute-v1",
        ),
    )
    assert result.execute("SELECT motion_activities FROM base.location_point").fetchone() == (
        ["unknown", None],
    )
    assert result.execute("SELECT is_valid FROM base.iphone_screen_time_event").fetchone() == (
        False,
    )
    assert result.execute("SELECT is_main_sleep, start_at FROM base.sleep_session").fetchone() == (
        None,
        datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC),
    )
    assert result.execute("SELECT cardio_fitness_level FROM base.health_daily").fetchone() == (
        None,
    )
    old = duckdb.connect(str(source), read_only=True)
    expected = {
        "sleep_session": "SELECT record_id,start_at,end_at,source_date,sleep_minutes,sleep_type,is_main_sleep,minutes_awake::DOUBLE,minutes_in_sleep_period::DOUBLE,minutes_to_fall_asleep::DOUBLE,minutes_after_wake_up::DOUBLE,offset_seconds,end_offset_seconds FROM base.fitbit_sleep_session",
        "steps_interval": "SELECT record_id,start_at,end_at,source_date,value,offset_seconds,end_offset_seconds FROM base.fitbit_activity_interval WHERE metric='steps'",
        "active_zone_interval": "SELECT record_id,start_at,end_at,source_date,value,category,offset_seconds,end_offset_seconds FROM base.fitbit_activity_interval WHERE metric='active-zone-minutes'",
        "exercise_session": "SELECT record_id,start_at,end_at,source_date,exercise_type,display_name,active_duration_seconds,offset_seconds,end_offset_seconds FROM base.fitbit_exercise",
        "sedentary_interval": "SELECT record_id,start_at,end_at,source_date,offset_seconds,end_offset_seconds FROM base.fitbit_sedentary_period",
        "distance_interval": "SELECT record_id,start_at,end_at,source_date,distance_meters,offset_seconds,end_offset_seconds FROM base.fitbit_distance_interval",
        "heart_rate_zone_interval": "SELECT record_id,start_at,end_at,source_date,zone,offset_seconds,end_offset_seconds FROM base.fitbit_heart_rate_zone_interval",
        "hrv_sample": "SELECT record_id,sample_at,source_date,rmssd_ms,sdnn_ms,offset_seconds FROM base.fitbit_hrv_sample",
        "sleep_respiratory_summary": "SELECT record_id,sample_at,source_date,full_breaths_per_minute,full_standard_deviation,full_signal_to_noise,deep_breaths_per_minute,deep_standard_deviation,deep_signal_to_noise,light_breaths_per_minute,light_standard_deviation,light_signal_to_noise,rem_breaths_per_minute,rem_standard_deviation,rem_signal_to_noise,offset_seconds FROM base.fitbit_respiratory_rate_sleep_summary",
        "location_point": "SELECT event_key,device_key,event_at,latitude,longitude,accuracy_m,velocity_kmh,motionactivities FROM base.owntracks_location",
        "iphone_screen_time_event": "SELECT event_key,device_key,bundle_id,event_at,state,is_active FROM base.iphone_screen_time_event",
        "mac_screen_time_event": "SELECT event_key,device_key,bundle_id,event_at,state,is_active FROM base.mac_screen_time_event",
        "health_daily": "SELECT r.source_date,r.beats_per_minute,h.rmssd_ms,h.deep_sleep_rmssd_ms,h.non_rem_heart_rate_bpm,h.entropy,b.breaths_per_minute,o.average_percent,o.lower_bound_percent,o.upper_bound_percent,o.standard_deviation_percent,t.nightly_temperature_celsius,t.baseline_temperature_celsius,t.relative_nightly_stddev_30d_celsius,v.vo2_max_ml_kg_min,v.estimated,v.cardio_fitness_level,v.covariance FROM base.fitbit_resting_heart_rate_daily r JOIN base.fitbit_hrv_daily h USING(source_date) JOIN base.fitbit_respiratory_rate_daily b USING(source_date) JOIN base.fitbit_oxygen_saturation_daily o USING(source_date) JOIN base.fitbit_sleep_temperature_daily t USING(source_date) JOIN base.fitbit_vo2_max_daily v USING(source_date)",
    }
    for table, query in expected.items():
        assert (
            result.execute(f'SELECT * FROM base."{table}"').fetchall()
            == old.execute(query).fetchall()
        )
    assert (
        result.execute(
            "SELECT sleep_session_id,kind,start_at,end_at,category,offset_seconds,end_offset_seconds FROM base.sleep_detail"
        ).fetchall()
        == old.execute(
            "SELECT sleep_id,kind,start_at,end_at,category,offset_seconds,end_offset_seconds FROM base.fitbit_sleep_detail"
        ).fetchall()
    )
    assert (
        result.execute(
            "SELECT start_at,mean_bpm,min_bpm,max_bpm,aggregation_version FROM base.heart_rate_minute"
        ).fetchall()
        == old.execute(
            "SELECT start_at,average,minimum,maximum,aggregation_version FROM base.fitbit_heart_rate_minute"
        ).fetchall()
    )
    old.close()
    result.close()
    import shutil

    copy = tmp_path / "renamed.duckdb"
    shutil.copyfile(target, copy)
    fresh = duckdb.connect(str(copy), read_only=True)
    for name in (
        "iphone_screen_time_interval",
        "mac_screen_time_interval",
        "daily_steps",
        "daily_active_zone",
        "daily_iphone_screen_time_total",
        "daily_mac_screen_time_total",
        "daily_sleep_detail",
    ):
        fresh.execute(f'SELECT count(*) FROM marts."{name}"').fetchone()
    fresh.close()


def test_cli_dry_run_leaves_source_and_outputs_untouched(tmp_path):
    import hashlib
    import os
    import sqlite3
    import subprocess
    import sys

    from scripts.analytics_migration_preflight import LEGACY_SQLITE_SCHEMA

    source, target = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    legacy_file(source)
    iphone, mac = tmp_path / "iphone.db", tmp_path / "mac.db"
    for path in (iphone, mac):
        with sqlite3.connect(path) as db:
            db.executescript(LEGACY_SQLITE_SCHEMA)
    before = {path: hashlib.sha256(path.read_bytes()).digest() for path in (source, iphone, mac)}
    outputs = [tmp_path / "i" / "collector.db", tmp_path / "m" / "collector.db"]
    command = [
        sys.executable,
        "-m",
        "scripts.migrate_analytics_schema",
        "--source-db",
        str(source),
        "--target-db",
        str(target),
        "--iphone-state",
        str(iphone),
        "--mac-state",
        str(mac),
        "--iphone-output",
        str(outputs[0]),
        "--mac-output",
        str(outputs[1]),
    ]
    subprocess.run(
        command,
        check=True,
        env={**os.environ, "PDP_FITBIT_SUBJECT_KEY": "account"},
        capture_output=True,
    )
    assert not target.exists()
    assert all(not path.parent.exists() for path in outputs)
    assert before == {path: hashlib.sha256(path.read_bytes()).digest() for path in before}


def test_cli_rejects_invalid_second_state_before_creating_stage(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    from scripts.analytics_migration_preflight import LEGACY_SQLITE_SCHEMA

    source, target = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    legacy_file(source)
    iphone, mac = tmp_path / "iphone.db", tmp_path / "mac.db"
    with sqlite3.connect(iphone) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
    with sqlite3.connect(mac) as db:
        db.execute("CREATE TABLE unexpected (x)")
    outputs = [tmp_path / "i" / "collector.db", tmp_path / "m" / "collector.db"]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.migrate_analytics_schema",
            "--source-db",
            str(source),
            "--target-db",
            str(target),
            "--iphone-state",
            str(iphone),
            "--mac-state",
            str(mac),
            "--iphone-output",
            str(outputs[0]),
            "--mac-output",
            str(outputs[1]),
            "--apply",
        ],
        env={**os.environ, "PDP_FITBIT_SUBJECT_KEY": "account"},
        capture_output=True,
    )
    assert result.returncode != 0
    assert not target.exists()
    assert all(not path.parent.exists() for path in outputs)


def test_interrupted_stage_rejects_blind_retry(tmp_path, monkeypatch):
    import scripts.migrate_analytics_schema as migration

    source, target = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    legacy_file(source)

    def interrupted(*args):
        raise RuntimeError("interruption")

    monkeypatch.setattr(migration, "_copy_projections", interrupted)
    with pytest.raises(RuntimeError, match="interruption"):
        migration.migrate_warehouse(source, target, subject="account", apply=True)
    assert target.exists()
    with pytest.raises(ValueError, match="target"):
        migration.migrate_warehouse(source, target, subject="account", apply=True)


def test_invalid_projection_fails_during_dry_run_before_writes(tmp_path, monkeypatch):
    import scripts.migrate_analytics_schema as migration

    source, target = tmp_path / "old.duckdb", tmp_path / "stage.duckdb"
    legacy_file(source)
    monkeypatch.setattr(
        migration,
        "PROJECTIONS",
        [
            (
                "steps_interval",
                "steps_interval_id",
                "SELECT unsupported FROM old.base.fitbit_activity_interval",
            )
        ],
    )
    with pytest.raises(duckdb.Error):
        migration.migrate_warehouse(source, target, subject="account", apply=False)
    assert not target.exists()


def test_colliding_projected_ids_fail_before_target_creation(tmp_path, monkeypatch):
    import scripts.migrate_analytics_schema as migration

    source, target = tmp_path / "old.duckdb", tmp_path / "stage.duckdb"
    legacy_file(source)
    connection = duckdb.connect(str(source))
    connection.execute(
        "INSERT INTO base.fitbit_resting_heart_rate_daily VALUES ('account','id1','2026-01-01',60,current_timestamp,'source',current_timestamp),('account','id2','2026-01-02',60,current_timestamp,'source',current_timestamp)"
    )
    connection.close()
    monkeypatch.setattr(
        migration,
        "PROJECTIONS",
        [
            (
                "steps_interval",
                "steps_interval_id",
                "SELECT 'collision' FROM old.base.fitbit_resting_heart_rate_daily",
            )
        ],
    )
    with pytest.raises(ValueError, match="identifier"):
        migration.migrate_warehouse(source, target, subject="account", apply=True)
    assert not target.exists()


def test_cli_rejects_segment_key_shared_across_os(tmp_path):
    import gzip
    import hashlib
    import os
    import sqlite3
    import subprocess
    import sys
    from datetime import UTC, datetime

    from personal_data_platform.biome.raw import ScreenTimeRawIdentity, format_observed_at
    from scripts.analytics_migration_preflight import LEGACY_SQLITE_SCHEMA

    source, target = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    legacy_file(source)
    iphone, mac = tmp_path / "iphone.db", tmp_path / "mac.db"
    for path, source_id, stream in (
        (iphone, "iphone_screen_time", "app-in-focus"),
        (mac, "mac_screen_time", "app-usage"),
    ):
        raw = b"x"
        digest = hashlib.sha256(raw).hexdigest()
        identity = ScreenTimeRawIdentity(
            source_id, "a" * 64, stream, "b" * 64, datetime(2026, 1, 1, tzinfo=UTC), digest
        )
        with sqlite3.connect(path) as db:
            db.executescript(LEGACY_SQLITE_SCHEMA)
            db.execute(
                "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status) VALUES (?,?,?,?,?,?,?,?)",
                (
                    identity.device_key,
                    stream,
                    identity.segment_key,
                    format_observed_at(identity.observed_at),
                    digest,
                    identity.object_key,
                    gzip.compress(raw, mtime=0),
                    "pending",
                ),
            )
    outputs = [tmp_path / "i" / "collector.db", tmp_path / "m" / "collector.db"]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.migrate_analytics_schema",
            "--source-db",
            str(source),
            "--target-db",
            str(target),
            "--iphone-state",
            str(iphone),
            "--mac-state",
            str(mac),
            "--iphone-output",
            str(outputs[0]),
            "--mac-output",
            str(outputs[1]),
        ],
        env={**os.environ, "PDP_FITBIT_SUBJECT_KEY": "account"},
        capture_output=True,
    )
    assert result.returncode != 0
    assert not target.exists()


def test_daily_union_keeps_all_null_day_and_missing_metrics(tmp_path):
    source, target = tmp_path / "old.duckdb", tmp_path / "stage.duckdb"
    legacy_file(source)
    connection = duckdb.connect(str(source))
    connection.execute(
        "INSERT INTO base.fitbit_hrv_daily (subject_key,record_id,source_date,fetched_at,source_key,loaded_at) VALUES ('account','hrv','2026-01-01',current_timestamp,'key',current_timestamp)"
    )
    connection.execute(
        "INSERT INTO base.fitbit_resting_heart_rate_daily VALUES ('account','heart','2026-01-02',61,current_timestamp,'key',current_timestamp)"
    )
    connection.close()
    migrate_warehouse(source, target, subject="account", apply=True)
    connection = duckdb.connect(str(target), read_only=True)
    assert connection.execute(
        "SELECT source_date::VARCHAR,resting_heart_rate_bpm,hrv_rmssd_ms FROM base.health_daily ORDER BY source_date"
    ).fetchall() == [("2026-01-01", None, None), ("2026-01-02", 61.0, None)]
    connection.close()


def test_cli_apply_stages_both_states_after_warehouse(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    from scripts.analytics_migration_preflight import LEGACY_SQLITE_SCHEMA

    source, target = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    legacy_file(source)
    iphone, mac = tmp_path / "iphone.db", tmp_path / "mac.db"
    for path in (iphone, mac):
        with sqlite3.connect(path) as db:
            db.executescript(LEGACY_SQLITE_SCHEMA)
    old_contents = {path: path.read_bytes() for path in (iphone, mac)}
    outputs = [tmp_path / "i" / "collector.db", tmp_path / "m" / "collector.db"]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.migrate_analytics_schema",
            "--source-db",
            str(source),
            "--target-db",
            str(target),
            "--iphone-state",
            str(iphone),
            "--mac-state",
            str(mac),
            "--iphone-output",
            str(outputs[0]),
            "--mac-output",
            str(outputs[1]),
            "--apply",
        ],
        env={**os.environ, "PDP_FITBIT_SUBJECT_KEY": "account"},
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert target.exists()
    for path in outputs:
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (1,)
            assert {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            } == {"file_state", "event_reference"}
    assert old_contents == {path: path.read_bytes() for path in (iphone, mac)}


@pytest.mark.parametrize(
    ("source_id", "platform", "stream"),
    [("iphone_screen_time", "ios", "app-in-focus"), ("mac_screen_time", "macos", "app-usage")],
)
@pytest.mark.parametrize("apply", [False, True])
def test_cli_rejects_active_unreferenced_event_before_all_outputs(
    tmp_path, source_id, platform, stream, apply
):
    import os
    import sqlite3
    import subprocess
    import sys

    from scripts.analytics_migration_preflight import LEGACY_SQLITE_SCHEMA

    source, target = tmp_path / "old.duckdb", tmp_path / "new.duckdb"
    legacy_file(source)
    connection = duckdb.connect(str(source))
    connection.execute(
        f'INSERT INTO base."{source_id}_event" (event_key,device_key,platform,source_stream,bundle_id,event_at,state,object_key,segment_key,observed_at,parser_version,unknown_field_count,duplicate_occurrence_count,is_active,loaded_at) '
        "VALUES ('orphan','device',?,?,'app',current_timestamp,'start','object','segment',current_timestamp,'v1',0,0,true,current_timestamp)",
        [platform, stream],
    )
    connection.close()
    iphone, mac = tmp_path / "iphone.db", tmp_path / "mac.db"
    for path in (iphone, mac):
        with sqlite3.connect(path) as db:
            db.executescript(LEGACY_SQLITE_SCHEMA)
    originals = {path: path.read_bytes() for path in (source, iphone, mac)}
    outputs = [tmp_path / "i" / "collector.db", tmp_path / "m" / "collector.db"]
    command = [
        sys.executable,
        "-m",
        "scripts.migrate_analytics_schema",
        "--source-db",
        str(source),
        "--target-db",
        str(target),
        "--iphone-state",
        str(iphone),
        "--mac-state",
        str(mac),
        "--iphone-output",
        str(outputs[0]),
        "--mac-output",
        str(outputs[1]),
    ]
    if apply:
        command.append("--apply")
    result = subprocess.run(
        command, env={**os.environ, "PDP_FITBIT_SUBJECT_KEY": "account"}, capture_output=True
    )
    assert result.returncode != 0
    assert b"active Screen Time event has no physical reference" in result.stderr
    assert not target.exists()
    assert all(not path.parent.exists() for path in outputs)
    assert originals == {path: path.read_bytes() for path in originals}

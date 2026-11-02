"""Explicit projections from the retired warehouse into the analytics baseline."""

import sqlite3
from pathlib import Path
from typing import Any

import duckdb

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from scripts.analytics_migration_preflight import validate_legacy

PROJECTIONS = [
    (
        "sleep_session",
        "sleep_session_id, start_at, end_at, source_date, sleep_minutes, sleep_type, is_main_sleep, "
        "awake_minutes, sleep_period_minutes, sleep_latency_minutes, after_wake_minutes, offset_seconds, "
        "end_offset_seconds",
        "SELECT record_id, start_at, end_at, source_date, sleep_minutes, sleep_type,\n"
        "       is_main_sleep, minutes_awake::DOUBLE, minutes_in_sleep_period::DOUBLE,\n"
        "       minutes_to_fall_asleep::DOUBLE, minutes_after_wake_up::DOUBLE,\n"
        "       offset_seconds, end_offset_seconds\n"
        "FROM old.base.fitbit_sleep_session",
    ),
    (
        "sleep_detail",
        "sleep_detail_id, sleep_session_id, kind, start_at, end_at, category, offset_seconds, "
        "end_offset_seconds",
        "SELECT sha256(to_json([kind, record_id])), sleep_id, kind, start_at, end_at, category,\n"
        "       offset_seconds, end_offset_seconds\n"
        "FROM old.base.fitbit_sleep_detail",
    ),
    (
        "heart_rate_minute",
        "heart_rate_minute_id, start_at, mean_bpm, min_bpm, max_bpm, aggregation_version",
        "SELECT sha256(to_json([data_source_family, strftime(start_at AT TIME ZONE 'UTC', "
        "'%Y-%m-%dT%H:%M:%S.%f') || '+00:00', aggregation_version])), start_at, average, minimum, "
        "maximum, aggregation_version FROM old.base.fitbit_heart_rate_minute",
    ),
    (
        "steps_interval",
        "steps_interval_id, start_at, end_at, source_date, steps, offset_seconds, end_offset_seconds",
        "SELECT record_id, start_at, end_at, source_date, value, offset_seconds, end_offset_seconds\n"
        "FROM old.base.fitbit_activity_interval WHERE metric='steps'",
    ),
    (
        "active_zone_interval",
        "active_zone_interval_id, start_at, end_at, source_date, active_zone_minutes, category, "
        "offset_seconds, end_offset_seconds",
        "SELECT record_id, start_at, end_at, source_date, value, category,\n"
        "       offset_seconds, end_offset_seconds\n"
        "FROM old.base.fitbit_activity_interval WHERE metric='active-zone-minutes'",
    ),
    (
        "exercise_session",
        "exercise_session_id, start_at, end_at, source_date, exercise_type, exercise_name, "
        "active_seconds, offset_seconds, end_offset_seconds",
        "SELECT record_id, start_at, end_at, source_date, exercise_type, display_name,\n"
        "       active_duration_seconds, offset_seconds, end_offset_seconds\n"
        "FROM old.base.fitbit_exercise",
    ),
    (
        "sedentary_interval",
        "sedentary_interval_id, start_at, end_at, source_date, offset_seconds, end_offset_seconds",
        "SELECT record_id, start_at, end_at, source_date, offset_seconds, end_offset_seconds\n"
        "FROM old.base.fitbit_sedentary_period",
    ),
    (
        "distance_interval",
        "distance_interval_id, start_at, end_at, source_date, distance_meters, offset_seconds, "
        "end_offset_seconds",
        "SELECT record_id, start_at, end_at, source_date, distance_meters,\n"
        "       offset_seconds, end_offset_seconds\n"
        "FROM old.base.fitbit_distance_interval",
    ),
    (
        "heart_rate_zone_interval",
        "heart_rate_zone_interval_id, start_at, end_at, source_date, zone, offset_seconds, "
        "end_offset_seconds",
        "SELECT record_id, start_at, end_at, source_date, zone, offset_seconds, end_offset_seconds\n"
        "FROM old.base.fitbit_heart_rate_zone_interval",
    ),
    (
        "health_daily",
        "source_date, resting_heart_rate_bpm, hrv_rmssd_ms, deep_sleep_hrv_rmssd_ms, "
        "non_rem_heart_rate_bpm, hrv_entropy, respiratory_rate_breaths_per_minute, spo2_average_percent, "
        "spo2_lower_bound_percent, spo2_upper_bound_percent, spo2_standard_deviation_percent, "
        "nightly_skin_temperature_celsius, baseline_skin_temperature_celsius, "
        "relative_nightly_stddev_30d_celsius, vo2_max_ml_kg_min, vo2_max_estimated, "
        "cardio_fitness_level, vo2_max_covariance",
        "WITH dates AS (\n"
        "  SELECT source_date FROM old.base.fitbit_resting_heart_rate_daily\n"
        "  UNION SELECT source_date FROM old.base.fitbit_hrv_daily\n"
        "  UNION SELECT source_date FROM old.base.fitbit_respiratory_rate_daily\n"
        "  UNION SELECT source_date FROM old.base.fitbit_oxygen_saturation_daily\n"
        "  UNION SELECT source_date FROM old.base.fitbit_sleep_temperature_daily\n"
        "  UNION SELECT source_date FROM old.base.fitbit_vo2_max_daily\n"
        ")\n"
        "SELECT d.source_date, r.beats_per_minute, h.rmssd_ms, h.deep_sleep_rmssd_ms,\n"
        "       h.non_rem_heart_rate_bpm, h.entropy, b.breaths_per_minute,\n"
        "       o.average_percent, o.lower_bound_percent, o.upper_bound_percent,\n"
        "       o.standard_deviation_percent, t.nightly_temperature_celsius,\n"
        "       t.baseline_temperature_celsius, t.relative_nightly_stddev_30d_celsius,\n"
        "       v.vo2_max_ml_kg_min, v.estimated, v.cardio_fitness_level, v.covariance\n"
        "FROM dates d\n"
        "LEFT JOIN old.base.fitbit_resting_heart_rate_daily r USING(source_date)\n"
        "LEFT JOIN old.base.fitbit_hrv_daily h USING(source_date)\n"
        "LEFT JOIN old.base.fitbit_respiratory_rate_daily b USING(source_date)\n"
        "LEFT JOIN old.base.fitbit_oxygen_saturation_daily o USING(source_date)\n"
        "LEFT JOIN old.base.fitbit_sleep_temperature_daily t USING(source_date)\n"
        "LEFT JOIN old.base.fitbit_vo2_max_daily v USING(source_date)",
    ),
    (
        "hrv_sample",
        "hrv_sample_id, sample_at, source_date, rmssd_ms, sdnn_ms, offset_seconds",
        "SELECT record_id, sample_at, source_date, rmssd_ms, sdnn_ms, offset_seconds\n"
        "FROM old.base.fitbit_hrv_sample",
    ),
    (
        "sleep_respiratory_summary",
        "respiratory_summary_id, sample_at, source_date, full_breaths_per_minute, "
        "full_standard_deviation, full_signal_to_noise, deep_breaths_per_minute, "
        "deep_standard_deviation, deep_signal_to_noise, light_breaths_per_minute, "
        "light_standard_deviation, light_signal_to_noise, rem_breaths_per_minute, "
        "rem_standard_deviation, rem_signal_to_noise, offset_seconds",
        "SELECT record_id, sample_at, source_date,\n"
        "       full_breaths_per_minute, full_standard_deviation, full_signal_to_noise,\n"
        "       deep_breaths_per_minute, deep_standard_deviation, deep_signal_to_noise,\n"
        "       light_breaths_per_minute, light_standard_deviation, light_signal_to_noise,\n"
        "       rem_breaths_per_minute, rem_standard_deviation, rem_signal_to_noise, offset_seconds\n"
        "FROM old.base.fitbit_respiratory_rate_sleep_summary",
    ),
    (
        "location_point",
        "location_point_id, device_key, observed_at, latitude, longitude, accuracy_meters, speed_kmh, "
        "motion_activities",
        "SELECT event_key, device_key, event_at, latitude, longitude,\n"
        "       accuracy_m, velocity_kmh, motionactivities\n"
        "FROM old.base.owntracks_location",
    ),
    (
        "iphone_screen_time_event",
        "event_key, device_key, bundle_id, event_at, state, is_valid",
        "SELECT event_key, device_key, bundle_id, event_at, state, is_active\n"
        "FROM old.base.iphone_screen_time_event",
    ),
    (
        "mac_screen_time_event",
        "event_key, device_key, bundle_id, event_at, state, is_valid",
        "SELECT event_key, device_key, bundle_id, event_at, state, is_active\n"
        "FROM old.base.mac_screen_time_event",
    ),
]


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def validate_projection_bindings(connection: Any, source_catalog: str) -> None:
    for _, columns, projection in PROJECTIONS:
        query = projection.replace("old.base.", f"{_identifier(source_catalog)}.base.")
        description = connection.execute("DESCRIBE " + query).fetchall()
        names = [part.strip() for part in columns.split(",")]
        if len(description) != len(names):
            raise ValueError("projection column count mismatch")
        identifiers = ",".join(_identifier(name) for name in names)
        key = _identifier(names[0])
        duplicates = connection.execute(
            f"SELECT count(*) FROM (SELECT {key} FROM ({query}) AS projected({identifiers}) "
            f"GROUP BY {key} HAVING {key} IS NULL OR count(*) > 1)"
        ).fetchone()[0]
        if duplicates:
            raise ValueError("projected identifier is null or duplicated")


def _copy_projections(connection: Any, source_catalog: str, target_catalog: str) -> None:
    source, target = _identifier(source_catalog), _identifier(target_catalog)
    connection.execute("BEGIN")
    try:
        for name, columns, original in PROJECTIONS:
            projection = original.replace("old.base.", f"{source}.base.")
            relation = f"{target}.base.{_identifier(name)}"
            connection.execute(f"INSERT INTO {relation} ({columns}) {projection}")
            left = connection.execute(
                f"SELECT count(*) FROM (({projection}) EXCEPT ALL "
                f"(SELECT {columns} FROM {relation}))"
            ).fetchone()[0]
            right = connection.execute(
                f"SELECT count(*) FROM ((SELECT {columns} FROM {relation}) "
                f"EXCEPT ALL ({projection}))"
            ).fetchone()[0]
            if left or right:
                raise ValueError(f"projection parity failed for {name}")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def migrate_warehouse(source: Path, target: Path, *, subject: str, apply: bool) -> None:
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise ValueError("source and target must differ")
    if target.exists():
        raise ValueError("target already exists")
    if not source.is_file():
        raise ValueError("source database does not exist")
    connection = duckdb.connect()
    try:
        connection.execute(f"ATTACH {_literal(str(source))} AS old (READ_ONLY)")
        connection.execute("USE old")
        validate_legacy(connection, subject=subject)
        validate_projection_bindings(connection, "old")
        if not apply:
            return
        stage = Warehouse(connect(WarehouseConfig(str(target))))
        try:
            stage.migrate()
        finally:
            stage.close()
        connection.execute(f"ATTACH {_literal(str(target))} AS new")
        _copy_projections(connection, "old", "new")
    finally:
        connection.close()
    run_dbt(target="local", config=WarehouseConfig(str(target)), timeout_seconds=300)
    stage_connection = duckdb.connect(str(target), read_only=True)
    try:
        current = stage_connection.execute("SELECT current_database()").fetchone()
        if current is None:
            raise ValueError("stage catalog is unavailable")
        verify_stage(stage_connection, current[0])
    finally:
        stage_connection.close()


def sqlite_readonly(path: Path) -> sqlite3.Connection:
    import sqlite3

    for suffix in ("-wal", "-journal"):
        if Path(str(path) + suffix).exists():
            raise ValueError("SQLite journal requires inspection before migration")
    return sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)


def migrate_state(
    warehouse: Any, source: Path | str, target: Path | str, *, source_id: str, apply: bool
) -> None:
    import gzip
    import hashlib
    import os
    import sqlite3
    import tempfile
    import zlib

    from personal_data_platform.biome.collector_state import _SCHEMA
    from personal_data_platform.biome.raw import parse_observed_at, parse_raw_object_key
    from scripts.analytics_migration_preflight import validate_legacy_sqlite

    streams = {"iphone_screen_time": "app-in-focus", "mac_screen_time": "app-usage"}
    if source_id not in streams:
        raise ValueError("unsupported source")
    source, target = Path(source).resolve(), Path(target).resolve()
    payload_dir = target.with_suffix(".payloads")
    if source == target or target.exists() or payload_dir.exists():
        raise ValueError("local state target already exists or equals source")
    if not source.is_file():
        raise ValueError("local state source missing")
    stream = streams[source_id]
    with sqlite_readonly(source) as old:
        validate_legacy_sqlite(old)
        observations = old.execute(
            "SELECT device_key,stream,segment_key,observed_at,sha256,object_key,"
            "CASE WHEN status='pending' THEN compressed_payload ELSE NULL END,status,storage_generation "
            "FROM segment_observation ORDER BY observed_at,object_key"
        ).fetchall()
    segment_rows = warehouse.execute(
        f"SELECT device_key,source_stream,segment_key,observed_at,object_key "
        f'FROM ops."{source_id}_segment"'
    ).fetchall()
    segment_map = {}
    for device, source_stream, segment, observed, key in segment_rows:
        if source_stream != stream or segment in segment_map:
            raise ValueError("inconsistent warehouse segment identity")
        segment_map[segment] = (device, observed, key)
    bad = warehouse.execute(
        f'SELECT count(*) FROM ops."{source_id}_record" r '
        f'LEFT JOIN ops."{source_id}_segment" s '
        "ON r.device_key=s.device_key AND r.source_stream=s.source_stream AND r.segment_key=s.segment_key "
        f'LEFT JOIN base."{source_id}_event" e ON r.event_key=e.event_key '
        "WHERE s.segment_key IS NULL OR e.event_key IS NULL "
        "OR r.device_key IS DISTINCT FROM e.device_key OR r.bundle_id IS DISTINCT FROM e.bundle_id "
        "OR r.event_at IS DISTINCT FROM e.event_at OR r.state IS DISTINCT FROM e.state"
    ).fetchone()[0]
    if bad:
        raise ValueError("orphaned or inconsistent Screen Time reference")
    orphaned_active = warehouse.execute(
        f'SELECT count(*) FROM base."{source_id}_event" e '
        f'WHERE e.is_active AND NOT EXISTS (SELECT 1 FROM ops."{source_id}_record" r '
        "WHERE r.event_key=e.event_key)"
    ).fetchone()[0]
    if orphaned_active:
        raise ValueError("active Screen Time event has no physical reference")
    refs = warehouse.execute(
        f'SELECT DISTINCT segment_key,event_key FROM ops."{source_id}_record" '
        "ORDER BY segment_key,event_key"
    ).fetchall()
    latest = {}
    rows = []
    key_devices: dict[str, str] = {}
    observation_times = set()
    for (
        device,
        actual_stream,
        segment,
        stamp,
        digest,
        key,
        payload,
        status,
        generation,
    ) in observations:
        if not isinstance(generation, int) or generation < 1:
            raise ValueError("invalid legacy storage generation")
        try:
            observed = parse_observed_at(stamp)
            raw = parse_raw_object_key(
                key, source_id=source_id, storage_created_at=observed, storage_generation=1
            )
        except ValueError:
            raise ValueError("invalid local snapshot identity") from None
        if (
            actual_stream,
            raw.subject_key,
            raw.stream,
            raw.logical_key,
            raw.observed_at,
            raw.sha256,
        ) != (stream, device, stream, segment, observed, digest):
            raise ValueError("inconsistent local snapshot identity")
        if (segment, observed) in observation_times:
            raise ValueError("duplicate observation time for a segment")
        observation_times.add((segment, observed))
        if segment in key_devices and key_devices[segment] != device:
            raise ValueError("segment belongs to multiple devices")
        key_devices[segment] = device
        if segment in segment_map and segment_map[segment][0] != device:
            raise ValueError("local and warehouse segment device mismatch")
        if status == "pending":
            try:
                if (
                    payload is None
                    or hashlib.sha256(gzip.decompress(payload)).hexdigest() != digest
                ):
                    raise ValueError("pending payload hash mismatch")
            except (OSError, EOFError, zlib.error) as error:
                raise ValueError("invalid pending payload") from error
        elif status == "uploaded":
            latest[segment] = (device, observed, key, digest)
        else:
            raise ValueError("unknown snapshot status")
        rows.append((device, segment, observed, digest, key, payload, status))
    for _, segment, observed, _, _, _, status in rows:
        if status == "pending" and segment in latest and observed <= latest[segment][1]:
            raise ValueError("pending snapshot precedes or ties sent snapshot")
        if status == "pending" and segment in segment_map and observed < segment_map[segment][1]:
            raise ValueError("pending snapshot precedes warehouse-confirmed snapshot")
    if not set(latest).issubset(segment_map):
        raise ValueError("warehouse and local sent segments differ")
    by_key = {row[4]: row for row in rows}
    parser_by_key = {}
    required_keys = {item[2] for item in latest.values()} | {
        item[2] for item in segment_map.values()
    }
    receipt_map: dict[str, tuple[Any, ...]] = {}
    ordered_keys = sorted(required_keys)
    for offset in range(0, len(ordered_keys), 500):
        batch = ordered_keys[offset : offset + 500]
        marks = ",".join("?" for _ in batch)
        receipt_rows = warehouse.execute(
            "SELECT object_key,schema_version,subject_key,source_stream,logical_key,observed_at,"
            "content_sha256,status,parser_version FROM ops.ingestion_metadata "
            f"WHERE source_id=? AND object_key IN ({marks})",
            [source_id, *batch],
        ).fetchall()
        receipt_map.update((row[0], row[1:]) for row in receipt_rows)

    def receipt_parser(device: Any, segment: Any, observed: Any, key: Any, digest: Any) -> str:
        receipt = receipt_map.get(key)
        if (
            receipt is None
            or receipt[:7] != (2, device, stream, segment, observed, digest, "succeeded")
            or not isinstance(receipt[7], str)
            or not receipt[7]
        ):
            raise ValueError("snapshot requires matching successful receipt and parser")
        return receipt[7]

    for segment, (device, observed, key, digest) in latest.items():
        parser_by_key[key] = receipt_parser(device, segment, observed, key, digest)
    for segment, (device, observed, key) in segment_map.items():
        row = by_key.get(key)
        if row is None or (row[0], row[1], row[2]) != (device, segment, observed):
            raise ValueError("warehouse and local segment snapshots differ")
        if row[6] == "uploaded" and latest.get(segment, (None, None, None))[2] != key:
            raise ValueError("warehouse segment references an obsolete uploaded snapshot")
        parser_by_key[key] = receipt_parser(device, segment, observed, key, row[3])
    mismatch = warehouse.execute(
        f'SELECT count(*) FROM ops."{source_id}_record" r '
        f'JOIN ops."{source_id}_segment" s USING(device_key,source_stream,segment_key) '
        "LEFT JOIN ops.ingestion_metadata m ON s.object_key=m.object_key "
        "WHERE r.parser_version IS DISTINCT FROM m.parser_version"
    ).fetchone()[0]
    if mismatch:
        raise ValueError("record and receipt parser mismatch")
    if not apply:
        return
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".migration-", dir=target.parent) as temporary:
        staging = Path(temporary)
        stage_db = staging / "collector.db"
        stage_payloads = staging / "collector.payloads"
        stage_payloads.mkdir(mode=0o700)
        with sqlite3.connect(stage_db) as output:
            os.chmod(stage_db, 0o600)
            output.executescript(_SCHEMA)
            for device, segment, observed, digest, key, payload, status in rows:
                if status == "uploaded" and latest[segment][2] != key:
                    continue
                path = None
                if status == "pending":
                    name = hashlib.sha256(key.encode()).hexdigest() + ".segb.gz"
                    with open(stage_payloads / name, "xb") as file:
                        os.chmod(file.name, 0o600)
                        file.write(payload)
                        file.flush()
                        os.fsync(file.fileno())
                    path = str(payload_dir / name)
                output.execute(
                    "INSERT INTO file_state VALUES (?,?,?,?,?,?,?,?)",
                    (
                        key,
                        segment,
                        device,
                        digest,
                        observed.isoformat(),
                        parser_by_key.get(key),
                        "sent" if status == "uploaded" else "pending",
                        path,
                    ),
                )
            output.executemany("INSERT INTO event_reference VALUES (?,?)", refs)
            if output.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("new SQLite integrity check failed")
            if (
                output.execute(
                    "SELECT segment_key,event_key FROM event_reference ORDER BY 1,2"
                ).fetchall()
                != refs
            ):
                raise ValueError("reference parity failed")
            actual = output.execute(
                "SELECT snapshot_key,segment_key,device_key,content_sha256,observed_at,parser_version,status,payload_path "
                "FROM file_state ORDER BY observed_at,snapshot_key"
            ).fetchall()
            expected = []
            for device, segment, observed, digest, key, payload, status in rows:
                if status == "uploaded" and latest[segment][2] != key:
                    continue
                name = hashlib.sha256(key.encode()).hexdigest() + ".segb.gz"
                path = str(payload_dir / name) if status == "pending" else None
                if status == "pending" and (stage_payloads / name).read_bytes() != payload:
                    raise ValueError("pending payload parity failed")
                expected.append(
                    (
                        key,
                        segment,
                        device,
                        digest,
                        observed.isoformat(),
                        parser_by_key.get(key),
                        "sent" if status == "uploaded" else "pending",
                        path,
                    )
                )
            if actual != expected:
                raise ValueError("local snapshot parity failed")
            output.execute("PRAGMA user_version=1")
        fd = os.open(stage_payloads, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        with open(stage_db, "rb") as database_file:
            os.fsync(database_file.fileno())
        if target.exists() or payload_dir.exists():
            raise ValueError("local state target appeared during staging")
        payload_dir.mkdir(mode=0o700)
        for payload in stage_payloads.iterdir():
            os.link(payload, payload_dir / payload.name)
        fd = os.open(payload_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(stage_db, target)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def assert_empty_catalog(connection: Any, catalog: str) -> None:
    objects = connection.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_catalog=?", [catalog]
    ).fetchone()[0]
    macros = connection.execute(
        "SELECT count(*) FROM duckdb_functions() WHERE database_name=? "
        "AND function_type IN ('macro','table_macro') AND NOT internal",
        [catalog],
    ).fetchone()[0]
    sequences = connection.execute(
        "SELECT count(*) FROM duckdb_sequences() WHERE database_name=?", [catalog]
    ).fetchone()[0]
    schemas = connection.execute(
        "SELECT count(*) FROM duckdb_schemas() WHERE database_name=? "
        "AND schema_name <> 'main' AND NOT internal",
        [catalog],
    ).fetchone()[0]
    if objects or macros or sequences or schemas:
        raise ValueError("target catalog is not empty")


def _connect_remote(source: str, target: str, token: str | None) -> Any:
    import re

    for name in (source, target):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid database name")
    if source in {"memory", "system", "temp"} or target in {
        source,
        "memory",
        "system",
        "temp",
        "personal_data_platform",
        "pdp_analytics",
    }:
        raise ValueError("source and target must differ and target must be a staging database")
    if not token:
        raise ValueError("MotherDuck token is required")
    connection = duckdb.connect(
        "md:",
        config={"motherduck_token": token, "motherduck_attach_mode": "workspace"},
    )
    try:
        databases = {
            row[0]
            for row in connection.execute("SELECT database_name FROM duckdb_databases()").fetchall()
        }
        if source not in databases or target not in databases:
            raise ValueError("source and stage must already exist")
        connection.execute(f"USE {_identifier(source)}")
    except BaseException:
        connection.close()
        raise
    return connection


def migrate_remote(
    source: str, target: str, *, subject: str, token: str | None, apply: bool
) -> None:
    from personal_data_platform.dbt_runner import run_dbt_from_env

    connection = _connect_remote(source, target, token)
    try:
        validate_legacy(connection, subject=subject)
        validate_projection_bindings(connection, source)
        assert_empty_catalog(connection, target)
        if not apply:
            return
        stage = Warehouse(connect(WarehouseConfig(target, token)))
        try:
            stage.migrate()
        finally:
            stage.close()
        connection.close()
        connection = _connect_remote(source, target, token)
        _copy_projections(connection, source, target)
    finally:
        connection.close()
    run_dbt_from_env(config=WarehouseConfig(target, token))
    stage_connection = connect(WarehouseConfig(target, token))
    try:
        verify_stage(stage_connection, target)
    finally:
        stage_connection.close()


def validate_segment_identities(connection: Any, iphone: Path, mac: Path) -> None:

    mappings: dict[str, tuple[str, str]] = {}
    warehouse_rows = connection.execute(
        "SELECT segment_key,device_key,source_stream FROM ops.iphone_screen_time_segment "
        "UNION ALL SELECT segment_key,device_key,source_stream FROM ops.mac_screen_time_segment"
    ).fetchall()
    rows = list(warehouse_rows)
    for path in (iphone, mac):
        source = Path(path).resolve()
        if not source.is_file():
            raise ValueError("local state source missing")
        with sqlite_readonly(source) as db:
            rows.extend(
                db.execute(
                    "SELECT DISTINCT segment_key,device_key,stream FROM segment_observation"
                ).fetchall()
            )
    for segment, device, stream in rows:
        identity = (device, stream)
        if segment in mappings and mappings[segment] != identity:
            raise ValueError("segment key maps to multiple devices or streams")
        mappings[segment] = identity


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Prepare an analytics migration stage")
    parser.add_argument(
        "--source-db",
    )
    parser.add_argument(
        "--target-db",
    )
    parser.add_argument(
        "--iphone-state",
        type=Path,
    )
    parser.add_argument(
        "--mac-state",
        type=Path,
    )
    parser.add_argument(
        "--iphone-output",
        type=Path,
    )
    parser.add_argument(
        "--mac-output",
        type=Path,
    )
    parser.add_argument("--prepare-install", nargs=2, metavar=("STAGED", "CANONICAL"))
    parser.add_argument("--source-id", choices=("iphone_screen_time", "mac_screen_time"))
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.prepare_install:
        if not args.source_id:
            parser.error("--source-id is required for install preparation")
        try:
            prepare_install(*args.prepare_install, source_id=args.source_id, apply=args.apply)
        except ValueError as error:
            parser.exit(1, f"migration failed: {error}\n")
        except Exception:
            parser.exit(1, "install preparation failed; original state was retained\n")
        print(
            "install preparation complete" if args.apply else "install preparation preflight passed"
        )
        return
    if not all(
        (
            args.source_db,
            args.target_db,
            args.iphone_state,
            args.mac_state,
            args.iphone_output,
            args.mac_output,
        )
    ):
        parser.error("source, target, both state files and both output paths are required")
    subject = os.environ.get("PDP_FITBIT_SUBJECT_KEY", "")
    token = os.environ.get("MOTHERDUCK_TOKEN")
    paths = [
        args.iphone_state.resolve(),
        args.mac_state.resolve(),
        args.iphone_output.resolve(),
        args.mac_output.resolve(),
        args.iphone_output.resolve().with_suffix(".payloads"),
        args.mac_output.resolve().with_suffix(".payloads"),
    ]
    if not args.remote:
        paths += [Path(args.source_db).resolve(), Path(args.target_db).resolve()]
    if len(paths) != len(set(paths)):
        parser.error("source and destination paths must all differ")
    try:
        if args.remote:
            connection = _connect_remote(args.source_db, args.target_db, token)
            validate_legacy(connection, subject=subject)
            validate_projection_bindings(connection, args.source_db)
            assert_empty_catalog(connection, args.target_db)
        else:
            migrate_warehouse(
                Path(args.source_db), Path(args.target_db), subject=subject, apply=False
            )
            connection = duckdb.connect(str(Path(args.source_db).resolve()), read_only=True)
        try:
            validate_segment_identities(connection, args.iphone_state, args.mac_state)
            migrate_state(
                connection,
                args.iphone_state,
                args.iphone_output,
                source_id="iphone_screen_time",
                apply=False,
            )
            migrate_state(
                connection,
                args.mac_state,
                args.mac_output,
                source_id="mac_screen_time",
                apply=False,
            )
        finally:
            connection.close()
        if args.apply:
            if args.remote:
                migrate_remote(
                    args.source_db, args.target_db, subject=subject, token=token, apply=True
                )
                connection = _connect_remote(args.source_db, args.target_db, token)
            else:
                migrate_warehouse(
                    Path(args.source_db), Path(args.target_db), subject=subject, apply=True
                )
                connection = duckdb.connect(str(Path(args.source_db).resolve()), read_only=True)
            try:
                migrate_state(
                    connection,
                    args.iphone_state,
                    args.iphone_output,
                    source_id="iphone_screen_time",
                    apply=True,
                )
                migrate_state(
                    connection,
                    args.mac_state,
                    args.mac_output,
                    source_id="mac_screen_time",
                    apply=True,
                )
            finally:
                connection.close()
        print("staging complete" if args.apply else "preflight passed; no outputs created")
    except ValueError as error:
        parser.exit(1, f"migration failed: {error}\n")
    except Exception:
        parser.exit(
            1, "migration failed during database or file operation; stage is not approved\n"
        )


def verify_stage(connection: Any, catalog: str) -> None:
    import hashlib
    import re

    expected_base = {name for name, _, _ in PROJECTIONS} | {"app", "profile", "place"}
    tables = set(
        connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog=? "
            "AND table_schema='base' AND table_type='BASE TABLE'",
            [catalog],
        ).fetchall()
    )
    if {row[0] for row in tables} != expected_base:
        raise ValueError("stage base inventory mismatch")
    ops = connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_catalog=? "
        "AND table_schema='ops' AND table_type='BASE TABLE'",
        [catalog],
    ).fetchall()
    if {row[0] for row in ops} != {"job_lock", "schema_migration"}:
        raise ValueError("stage ops inventory mismatch")
    views = connection.execute(
        "SELECT view_name,sql FROM duckdb_views() WHERE database_name=? AND schema_name='marts' AND NOT internal",
        [catalog],
    ).fetchall()
    expected_views = {
        "iphone_screen_time_interval",
        "mac_screen_time_interval",
        "daily_steps",
        "daily_active_zone",
        "daily_iphone_screen_time_total",
        "daily_mac_screen_time_total",
        "daily_sleep_detail",
    }
    if {row[0] for row in views} != expected_views:
        raise ValueError("stage marts inventory mismatch")
    all_objects = set(
        connection.execute(
            "SELECT table_schema,table_name,table_type FROM information_schema.tables WHERE table_catalog=?",
            [catalog],
        ).fetchall()
    )
    expected_objects = (
        {("base", name, "BASE TABLE") for name in expected_base}
        | {("ops", name, "BASE TABLE") for name in ("job_lock", "schema_migration")}
        | {("marts", name, "VIEW") for name in expected_views}
    )
    if all_objects != expected_objects:
        raise ValueError("stage contains unexpected objects")
    schemas = connection.execute(
        "SELECT schema_name FROM duckdb_schemas() WHERE database_name=? AND NOT internal", [catalog]
    ).fetchall()
    if {row[0] for row in schemas} != {"base", "ops", "marts"}:
        raise ValueError("stage schema inventory mismatch")
    extras = connection.execute(
        "SELECT count(*) FROM duckdb_functions() WHERE database_name=? "
        "AND function_type IN ('macro','table_macro') AND NOT internal",
        [catalog],
    ).fetchone()[0]
    extras += connection.execute(
        "SELECT count(*) FROM duckdb_sequences() WHERE database_name=?", [catalog]
    ).fetchone()[0]
    if extras:
        raise ValueError("stage contains unexpected functions or sequences")
    prefix = _identifier(catalog)
    for name, sql in views:
        if re.search(r'(?i)(?<![\w])"?' + re.escape(catalog) + r'"?\s*\.', sql):
            raise ValueError("stage view depends on its catalog name")
        connection.execute(f"SELECT count(*) FROM {prefix}.marts.{_identifier(name)}").fetchone()
    for name in ("app", "profile", "place"):
        if connection.execute(f"SELECT count(*) FROM {prefix}.base.{_identifier(name)}").fetchone()[
            0
        ]:
            raise ValueError("unexpected static data in stage")
    if connection.execute(f"SELECT count(*) FROM {prefix}.ops.job_lock").fetchone()[0]:
        raise ValueError("stage writer lease is not clear")
    baseline = (
        Path(__file__).resolve().parents[1]
        / "src/personal_data_platform/migrations/001_initial.sql"
    )
    expected_checksum = hashlib.sha256(baseline.read_bytes()).hexdigest()
    history = connection.execute(
        f"SELECT migration_id,checksum FROM {prefix}.ops.schema_migration"
    ).fetchall()
    if history != [("001_initial.sql", expected_checksum)]:
        raise ValueError("stage baseline history mismatch")


def prepare_install(
    staged: Path | str, canonical: Path | str, *, source_id: str, apply: bool
) -> Path:
    import gzip
    import hashlib
    import os
    import sqlite3
    import tempfile
    import zlib
    from datetime import datetime

    from personal_data_platform.biome.raw import parse_raw_object_key

    streams = {"iphone_screen_time": "app-in-focus", "mac_screen_time": "app-usage"}
    if source_id not in streams:
        raise ValueError("unsupported source")
    staged, canonical = Path(staged).resolve(), Path(canonical).resolve()
    ready = canonical.with_name(canonical.name + ".ready")
    final_payloads = canonical.with_suffix(".payloads")
    staged_payloads = staged.with_suffix(".payloads").resolve()
    if staged == canonical or ready.exists() or final_payloads.exists():
        raise ValueError("install destination is occupied or equals stage")
    if not staged.is_file():
        raise ValueError("prepared state is missing")
    with sqlite_readonly(staged) as old:
        if old.execute("PRAGMA integrity_check").fetchone() != ("ok",) or old.execute(
            "PRAGMA user_version"
        ).fetchone() != (1,):
            raise ValueError("prepared state is not verified")
        tables = {
            row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if tables != {"file_state", "event_reference"}:
            raise ValueError("unsupported prepared state schema")
        rows = old.execute(
            "SELECT snapshot_key,segment_key,device_key,content_sha256,observed_at,parser_version,status,payload_path FROM file_state ORDER BY observed_at,snapshot_key"
        ).fetchall()
        pending = []
        for key, segment, device, digest, timestamp, parser, status, path in rows:
            try:
                observed = datetime.fromisoformat(timestamp)
                raw = parse_raw_object_key(
                    key, source_id=source_id, storage_created_at=observed, storage_generation=1
                )
            except (ValueError, TypeError):
                raise ValueError("invalid prepared snapshot identity") from None
            if (raw.stream, raw.logical_key, raw.subject_key, raw.sha256, raw.observed_at) != (
                streams[source_id],
                segment,
                device,
                digest,
                observed,
            ):
                raise ValueError("inconsistent prepared snapshot identity")
            if status == "sent":
                if path is not None or not parser:
                    raise ValueError("invalid prepared sent metadata")
                continue
            if status != "pending" or not path:
                raise ValueError("invalid prepared pending metadata")
            payload = Path(path)
            if payload.resolve().parent != staged_payloads or payload.is_symlink():
                raise ValueError("prepared payload path is outside staging")
            try:
                content = payload.read_bytes()
                if hashlib.sha256(gzip.decompress(content)).hexdigest() != digest:
                    raise ValueError("prepared payload hash mismatch")
            except (OSError, EOFError, zlib.error):
                raise ValueError("prepared payload unavailable or corrupt") from None
            pending.append((key, payload.name, content))
        if not apply:
            return ready
        canonical.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=canonical.parent) as temporary:
            temp_db = Path(temporary) / "collector.db"
            with sqlite3.connect(temp_db) as output:
                old.backup(output)
                os.chmod(temp_db, 0o600)
                output.executemany(
                    "UPDATE file_state SET payload_path=? WHERE snapshot_key=?",
                    [(str(final_payloads / name), key) for key, name, _ in pending],
                )
                output.commit()
                if output.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("rebased state integrity check failed")
            with open(temp_db, "rb") as file:
                os.fsync(file.fileno())
            final_payloads.mkdir(mode=0o700)
            for _, name, content in pending:
                descriptor = os.open(
                    final_payloads / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                with os.fdopen(descriptor, "wb") as output_file:
                    output_file.write(content)
                    output_file.flush()
                    os.fsync(output_file.fileno())
            fd = os.open(final_payloads, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(temp_db, ready)
            fd = os.open(canonical.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return ready


if __name__ == "__main__":
    main()

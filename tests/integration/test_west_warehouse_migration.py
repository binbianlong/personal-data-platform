from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse
from tests.screen_time_helpers import Repository, event, segb
from tests.unit.test_west_raw_migration import Client


def setup_source(source, raw_path):
    from scripts.migrate_west_raw import copy_manifest, inventory

    warehouse = Warehouse(source)
    warehouse.migrate()
    repository = Repository()
    raw = repository.add("100", segb(event("synthetic.app"))[0])
    codec = get_source("screen_time", "app-in-focus")
    compressed = repository.get_raw(raw.key, generation=1)
    import gzip

    warehouse.load_object(
        raw,
        byte_size=len(gzip.decompress(compressed)),
        batch=codec.decode(raw, gzip.decompress(compressed)),
    )
    client = Client()
    client.buckets["old"].objects[raw.key] = (
        compressed,
        raw.storage_generation,
        raw.storage_created_at,
        None,
    )
    inventory(client, source_bucket="old", target_bucket="west", manifest_path=raw_path)
    copy_manifest(client, raw_path)
    source.execute(
        "INSERT INTO ops.job_run VALUES ('screen-job', 'loader:screen_time:app-in-focus',"
        " 'succeeded', now(), now(), '{\"source_id\":\"screen_time\"}')"
    )
    source.execute(
        "INSERT INTO ops.job_run VALUES ('fitbit-job', 'loader:fitbit:steps',"
        " 'succeeded', now(), now(), '{\"source_id\":\"fitbit\"}')"
    )
    source.execute(
        "INSERT INTO ops.job_lock VALUES ('loader', 'old-owner', now() + INTERVAL 1 DAY)"
    )
    return raw


def test_export_import_preserves_screen_time_and_maps_raw_without_old_fitbit_state(tmp_path):
    from scripts.migrate_west_warehouse import export_snapshot, import_snapshot, verify_snapshot

    source = duckdb.connect()
    target = duckdb.connect()
    raw_path = tmp_path / "raw.json"
    raw = setup_source(source, raw_path)
    Warehouse(target).migrate(profile="west")
    target.execute(
        "INSERT INTO ops.fitbit_coverage VALUES ('new','steps', '2026-10-01'::TIMESTAMPTZ, '2026-10-02'::TIMESTAMPTZ, now(), 'api', 'new-key', 'hash', 'source-hash')"
    )
    source.execute("INSERT INTO ops.fitbit_notification VALUES ('old', 'old-person', now())")
    snapshot = tmp_path / "export.duckdb"
    export_snapshot(source, snapshot)
    import_snapshot(target, snapshot, raw_path)
    import_snapshot(target, snapshot, raw_path)
    assert verify_snapshot(target, snapshot, raw_path)["base.screen_time_event"] == 1
    row = target.execute(
        "SELECT storage_created_at, storage_generation, retention_started_at "
        "FROM ops.ingestion_metadata WHERE object_key = ?",
        [raw.key],
    ).fetchone()
    assert row == (datetime(2026, 10, 5, tzinfo=UTC), 101, raw.storage_created_at)
    assert target.execute("SELECT subject_key FROM ops.fitbit_coverage").fetchall() == [("new",)]
    assert target.execute("SELECT run_id FROM ops.job_run").fetchall() == [("screen-job",)]
    assert target.execute("SELECT count(*) FROM ops.job_lock").fetchone()[0] == 0
    assert target.execute(
        "SELECT migration_id FROM ops.schema_migration ORDER BY 1"
    ).fetchall() != (
        source.execute("SELECT migration_id FROM ops.schema_migration ORDER BY 1").fetchall()
    )
    source.close()
    target.close()


def test_final_delta_applies_updates_and_deletions_preserving_new_fitbit(tmp_path):
    from scripts.migrate_west_warehouse import export_snapshot, import_snapshot

    source, target = duckdb.connect(), duckdb.connect()
    raw_path = tmp_path / "raw.json"
    setup_source(source, raw_path)
    Warehouse(target).migrate(profile="west")
    snapshot = tmp_path / "export.duckdb"
    export_snapshot(source, snapshot)
    import_snapshot(target, snapshot, raw_path)
    source.execute("UPDATE base.screen_time_event SET bundle_id='corrected.app', is_active=false")
    source.execute("DELETE FROM ops.screen_time_record")
    source.execute("DELETE FROM ops.job_run WHERE run_id='screen-job'")
    target.execute(
        "INSERT INTO ops.fitbit_coverage VALUES ('new','steps', '2026-10-01'::TIMESTAMPTZ, '2026-10-02'::TIMESTAMPTZ, now(), 'api', 'new-key', 'hash', 'source-hash')"
    )
    export_snapshot(source, snapshot)
    import_snapshot(target, snapshot, raw_path)
    assert target.execute("SELECT bundle_id, is_active FROM base.screen_time_event").fetchall() == [
        ("corrected.app", False)
    ]
    assert target.execute("SELECT count(*) FROM ops.screen_time_record").fetchone()[0] == 0
    assert target.execute("SELECT subject_key FROM ops.fitbit_coverage").fetchall() == [("new",)]
    assert target.execute("SELECT count(*) FROM ops.job_run").fetchone()[0] == 0
    source.close()
    target.close()


@pytest.mark.parametrize("corruption", ["generation", "hash", "incomplete", "foreign_table"])
def test_import_rejects_invalid_raw_mapping_and_foreign_snapshot_without_changes(
    tmp_path, corruption
):
    from scripts.migrate_west_warehouse import export_snapshot, import_snapshot

    source, target = duckdb.connect(), duckdb.connect()
    raw_path = tmp_path / "raw.json"
    setup_source(source, raw_path)
    Warehouse(target).migrate(profile="west")
    snapshot = tmp_path / "export.duckdb"
    export_snapshot(source, snapshot)
    document = json.loads(raw_path.read_text())
    if corruption == "foreign_table":
        local = duckdb.connect(str(snapshot))
        local.execute("CREATE TABLE ops.fitbit_notification AS SELECT 'unsafe' AS notification_id")
        local.close()
    else:
        if corruption == "generation":
            document["objects"][0]["source_generation"] = 88
        elif corruption == "hash":
            document["objects"][0]["content_sha256"] = "a" * 64
        else:
            document["objects"][0]["target_generation"] = None
            document["objects"][0]["target_created_at"] = None
        raw_path.write_text(json.dumps(document))
    with pytest.raises((ValueError, RuntimeError)):
        import_snapshot(target, snapshot, raw_path)
    assert target.execute("SELECT count(*) FROM base.screen_time_event").fetchone()[0] == 0
    source.close()
    target.close()


def test_load_state_round_trip_keeps_retention_origin_for_success_and_failure():
    warehouse = Warehouse(duckdb.connect())
    warehouse.migrate()
    repository = Repository()
    raw = repository.add("100", segb(event("synthetic.app"))[0])
    raw = replace(raw, retention_started_at=raw.storage_created_at - timedelta(days=70))
    codec = get_source("screen_time", "app-in-focus")
    import gzip

    data = gzip.decompress(repository.get_raw(raw.key, generation=1))
    warehouse.load_object(raw, byte_size=len(data), batch=codec.decode(raw, data))
    assert (
        warehouse.active_ingestion_states(source_id="screen_time", stream="app-in-focus")[
            raw.key
        ].retention_started_at
        == raw.retention_started_at
    )
    warehouse.mark_failed(raw, byte_size=len(data), error=RuntimeError("synthetic failure"))
    assert (
        warehouse.active_ingestion_states(source_id="screen_time", stream="app-in-focus")[
            raw.key
        ].retention_started_at
        == raw.retention_started_at
    )
    warehouse.close()


def test_source_connection_enforces_read_only_and_export_is_private(tmp_path):
    from scripts.migrate_west_warehouse import _source_connection, main

    source_path = tmp_path / "source.duckdb"
    source = duckdb.connect(str(source_path))
    setup_source(source, tmp_path / "raw.json")
    source.close()
    source = _source_connection(str(source_path))
    with pytest.raises(duckdb.InvalidInputException):
        source.execute("DELETE FROM ops.ingestion_metadata")
    assert source.execute("SELECT count(*) FROM base.screen_time_event").fetchone()[0] == 1
    source.close()
    snapshot = tmp_path / "private.duckdb"
    assert main(["--export", "--source-db", str(source_path), "--snapshot", str(snapshot)]) == 0
    assert snapshot.stat().st_mode & 0o777 == 0o600


def test_cli_rejects_combined_cloud_credentials_before_connect(monkeypatch, tmp_path):
    from scripts import migrate_west_warehouse as migration

    def unexpected(*args, **kwargs):
        raise AssertionError("combined migration may not open either credential")

    monkeypatch.setattr(migration, "_source_connection", unexpected)
    with pytest.raises(SystemExit) as error:
        migration.main(
            [
                "--final-delta",
                "--source-db",
                "source",
                "--target-db",
                "target",
                "--snapshot",
                str(tmp_path / "private.duckdb"),
                "--raw-manifest",
                str(tmp_path / "raw.json"),
            ]
        )
    assert error.value.code == 2


def test_scripts_support_direct_python_invocation():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for name in ("migrate_west_raw.py", "migrate_west_warehouse.py"):
        result = subprocess.run(
            [sys.executable, str(root / "scripts" / name), "--help"], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        assert "--verify" in result.stdout


@pytest.mark.parametrize("duckling", ["source.rw", "source.rs.3", None])
def test_cloud_source_requires_verified_read_scaling_connection(monkeypatch, duckling):
    from scripts.migrate_west_warehouse import _source_connection

    class Connection:
        closed = False

        def execute(self, query):
            assert query == "SELECT * FROM __md_duckling_id()"
            return self

        def fetchone(self):
            return (duckling,) if duckling else None

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setenv("SOURCE_MOTHERDUCK_TOKEN", "synthetic-read-token")
    monkeypatch.setattr("scripts.migrate_west_warehouse.duckdb.connect", lambda *a, **k: connection)
    if duckling == "source.rs.3":
        assert _source_connection("source") is connection
    else:
        with pytest.raises(ValueError, match="read-scaling"):
            _source_connection("source")
        assert connection.closed


@pytest.mark.parametrize("active_leases", [0, 1])
def test_stopped_cloud_source_accepts_read_only_snapshot_only_without_active_leases(
    monkeypatch, active_leases
):
    from scripts.migrate_west_warehouse import _source_connection

    class Connection:
        closed = False

        def execute(self, query):
            self.query = query
            return self

        def fetchone(self):
            if self.query == "SELECT * FROM __md_duckling_id()":
                return ("source.rw",)
            return (active_leases,)

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setenv("SOURCE_MOTHERDUCK_TOKEN", "synthetic-read-token")

    def connect_read_only(database, *, read_only, config):
        assert read_only is True
        return connection

    monkeypatch.setattr("scripts.migrate_west_warehouse.duckdb.connect", connect_read_only)
    if active_leases:
        with pytest.raises(ValueError, match="active.*lease"):
            _source_connection("source", source_writers_stopped=True)
        assert connection.closed
    else:
        assert _source_connection("source", source_writers_stopped=True) is connection


def test_import_supports_source_without_retention_migration_and_respects_active_writer(tmp_path):
    from scripts.migrate_west_warehouse import export_snapshot, import_snapshot

    source, target = duckdb.connect(), duckdb.connect()
    raw_path = tmp_path / "raw.json"
    raw = setup_source(source, raw_path)
    source.execute("ALTER TABLE ops.ingestion_metadata DROP COLUMN retention_started_at")
    Warehouse(target).migrate(profile="west")
    snapshot = tmp_path / "export.duckdb"
    export_snapshot(source, snapshot)
    target.execute(
        "INSERT INTO ops.job_lock VALUES ('loader', 'active-owner', now() + INTERVAL 1 DAY)"
    )
    with pytest.raises(RuntimeError, match="another writer"):
        import_snapshot(target, snapshot, raw_path)
    assert target.execute("SELECT count(*) FROM base.screen_time_event").fetchone()[0] == 0
    target.execute("DELETE FROM ops.job_lock")
    import_snapshot(target, snapshot, raw_path)
    assert target.execute("SELECT retention_started_at FROM ops.ingestion_metadata").fetchone() == (
        raw.storage_created_at,
    )
    source.close()
    target.close()


def test_failed_import_rolls_back_every_table_and_can_be_restarted(tmp_path):
    from scripts.migrate_west_warehouse import export_snapshot, import_snapshot

    source, target = duckdb.connect(), duckdb.connect()
    raw_path = tmp_path / "raw.json"
    setup_source(source, raw_path)
    Warehouse(target).migrate(profile="west")
    snapshot = tmp_path / "export.duckdb"
    export_snapshot(source, snapshot)

    class InterruptedConnection:
        def execute(self, *args):
            result = target.execute(*args)
            if args[0].startswith("INSERT INTO base.screen_time_event"):
                raise RuntimeError("import interrupted")
            return result

    with pytest.raises(RuntimeError, match="import interrupted"):
        import_snapshot(InterruptedConnection(), snapshot, raw_path)
    assert target.execute("SELECT count(*) FROM base.screen_time_event").fetchone()[0] == 0
    assert target.execute("SELECT count(*) FROM ops.ingestion_metadata").fetchone()[0] == 0
    assert target.execute("SELECT count(*) FROM ops.job_lock").fetchone()[0] == 0
    import_snapshot(target, snapshot, raw_path)
    assert target.execute("SELECT count(*) FROM base.screen_time_event").fetchone()[0] == 1
    source.close()
    target.close()

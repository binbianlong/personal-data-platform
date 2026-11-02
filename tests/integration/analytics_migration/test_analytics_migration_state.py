import gzip
import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.biome.raw import ScreenTimeRawIdentity, format_observed_at
from scripts.analytics_migration_preflight import LEGACY_SQLITE_SCHEMA
from scripts.migrate_analytics_schema import migrate_state


def test_pending_payload_is_preserved_and_dry_run_creates_nothing(tmp_path):
    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "prepared" / "collector.db"
    data = b"original pending bytes"
    compressed = gzip.compress(data, mtime=0)
    identity = ScreenTimeRawIdentity(
        "iphone_screen_time",
        "a" * 64,
        "app-in-focus",
        "b" * 64,
        datetime(2026, 1, 1, tzinfo=UTC),
        hashlib.sha256(data).hexdigest(),
    )
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        db.execute(
            "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status) VALUES (?,?,?,?,?,?,?,?)",
            (
                identity.device_key,
                identity.stream,
                identity.segment_key,
                format_observed_at(identity.observed_at),
                identity.sha256,
                identity.object_key,
                compressed,
                "pending",
            ),
        )
    migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=False)
    assert not new.parent.exists()
    migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=True)
    state = CollectorState(new, source_id="iphone_screen_time")
    pending = state.pending()
    assert len(pending) == 1
    assert pending[0].identity == identity
    assert pending[0].compressed_payload == compressed
    assert pending[0].parser_version is None
    with sqlite3.connect(new) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
    warehouse.close()


@pytest.mark.parametrize(
    ("source_id", "stream"),
    [("iphone_screen_time", "app-in-focus"), ("mac_screen_time", "app-usage")],
)
@pytest.mark.parametrize("latest_status", ["uploaded", "pending"])
@pytest.mark.parametrize("apply", [False, True])
def test_pending_before_confirmed_warehouse_snapshot_rejected_before_output(
    tmp_path, source_id, stream, latest_status, apply
):
    from datetime import timedelta

    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "new" / "collector.db"
    device, segment = "a" * 64, "b" * 64
    first = datetime(2026, 1, 1, tzinfo=UTC)
    identities = []
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        for index, (data, status) in enumerate([(b"older", "pending"), (b"newer", latest_status)]):
            item = ScreenTimeRawIdentity(
                source_id,
                device,
                stream,
                segment,
                first + timedelta(seconds=index),
                hashlib.sha256(data).hexdigest(),
            )
            identities.append(item)
            db.execute(
                "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status) VALUES (?,?,?,?,?,?,?,?)",
                (
                    device,
                    stream,
                    segment,
                    format_observed_at(item.observed_at),
                    item.sha256,
                    item.object_key,
                    gzip.compress(data, mtime=0),
                    status,
                ),
            )
    confirmed = identities[1]
    warehouse.execute(
        f'INSERT INTO ops."{source_id}_segment" VALUES (?, ?, ?, ?, ?)',
        [device, stream, segment, confirmed.observed_at, confirmed.object_key],
    )
    warehouse.execute(
        "INSERT INTO ops.ingestion_metadata (object_key,source_id,schema_version,subject_key,source_stream,logical_key,observed_at,content_sha256,byte_size,status,parser_version,started_at) VALUES (?, ?, 2, ?, ?, ?, ?, ?, 5, 'succeeded', 'v1', ?)",
        [
            confirmed.object_key,
            source_id,
            device,
            stream,
            segment,
            confirmed.observed_at,
            confirmed.sha256,
            confirmed.observed_at,
        ],
    )
    before = old.read_bytes()
    with pytest.raises(ValueError, match="pending snapshot"):
        migrate_state(warehouse, old, new, source_id=source_id, apply=apply)
    assert not new.parent.exists()
    assert old.read_bytes() == before
    warehouse.close()


@pytest.mark.parametrize("active", [False, True])
def test_distinct_refs_preserve_multiple_segments_and_inactive_event(tmp_path, active):
    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "stage" / "collector.db"
    device, digest = "a" * 64, hashlib.sha256(b"x").hexdigest()
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    keys = []
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        for segment in ("b" * 64, "c" * 64):
            identity = ScreenTimeRawIdentity(
                "iphone_screen_time", device, "app-in-focus", segment, observed, digest
            )
            keys.append(identity)
            db.execute(
                "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,status) VALUES (?,?,?,?,?,?,?)",
                (
                    device,
                    identity.stream,
                    segment,
                    format_observed_at(observed),
                    digest,
                    identity.object_key,
                    "uploaded",
                ),
            )
            warehouse.execute(
                "INSERT INTO ops.iphone_screen_time_segment VALUES (?,?,?,?,?)",
                [device, identity.stream, segment, observed, identity.object_key],
            )
            warehouse.execute(
                "INSERT INTO ops.ingestion_metadata (object_key,source_id,schema_version,subject_key,source_stream,logical_key,observed_at,content_sha256,byte_size,status,parser_version,started_at) VALUES (?, 'iphone_screen_time', 2, ?, 'app-in-focus', ?, ?, ?, 1, 'succeeded', 'v1', ?)",
                [identity.object_key, device, segment, observed, digest, observed],
            )
    warehouse.execute(
        "INSERT INTO base.iphone_screen_time_event (event_key,device_key,platform,source_stream,bundle_id,event_at,state,object_key,segment_key,observed_at,parser_version,unknown_field_count,duplicate_occurrence_count,is_active,loaded_at) VALUES ('event', ?, 'ios', 'app-in-focus', 'app', ?, 'start', ?, ?, ?, 'v1', 0, 1, ?, ?)",
        [device, observed, keys[0].object_key, keys[0].segment_key, observed, active, observed],
    )
    for n, segment in enumerate([keys[0].segment_key, keys[0].segment_key, keys[1].segment_key]):
        warehouse.execute(
            "INSERT INTO ops.iphone_screen_time_record (physical_id,device_key,source_stream,segment_key,record_offset,record_metadata_offset,event_key,bundle_id,event_at,state,parser_version,unknown_field_count) VALUES (?,?,'app-in-focus',?,?,0,'event','app',?,'start','v1',0)",
            [f"p{n}", device, segment, n, observed],
        )
    migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=True)
    with sqlite3.connect(new) as db:
        assert db.execute(
            "SELECT segment_key,event_key FROM event_reference ORDER BY 1"
        ).fetchall() == [(item.segment_key, "event") for item in keys]
        assert db.execute(
            "SELECT count(*) FROM file_state WHERE status='sent' AND parser_version='v1'"
        ).fetchone() == (2,)
    warehouse.execute(
        "UPDATE ops.iphone_screen_time_record SET parser_version='v2' WHERE physical_id='p0'"
    )
    import pytest

    with pytest.raises(ValueError, match="parser"):
        migrate_state(
            warehouse,
            old,
            tmp_path / "other" / "collector.db",
            source_id="iphone_screen_time",
            apply=False,
        )
    warehouse.execute(
        "UPDATE ops.iphone_screen_time_record SET parser_version='v1' WHERE physical_id='p0'"
    )
    warehouse.execute(
        "UPDATE ops.iphone_screen_time_record SET bundle_id='wrong' WHERE physical_id='p1'"
    )
    with pytest.raises(ValueError, match="inconsistent Screen Time reference"):
        migrate_state(
            warehouse,
            old,
            tmp_path / "mismatch" / "collector.db",
            source_id="iphone_screen_time",
            apply=False,
        )
    assert not (tmp_path / "mismatch").exists()
    warehouse.close()


@pytest.mark.parametrize("active", [False, True])
def test_mac_distinct_refs_preserve_multiple_segments_and_inactive_event(tmp_path, active):
    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "stage" / "collector.db"
    device, digest = "a" * 64, hashlib.sha256(b"x").hexdigest()
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    keys = []
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        for segment in ("b" * 64, "c" * 64):
            identity = ScreenTimeRawIdentity(
                "mac_screen_time", device, "app-usage", segment, observed, digest
            )
            keys.append(identity)
            db.execute(
                "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,status) VALUES (?,?,?,?,?,?,?)",
                (
                    device,
                    identity.stream,
                    segment,
                    format_observed_at(observed),
                    digest,
                    identity.object_key,
                    "uploaded",
                ),
            )
            warehouse.execute(
                "INSERT INTO ops.mac_screen_time_segment VALUES (?,?,?,?,?)",
                [device, identity.stream, segment, observed, identity.object_key],
            )
            warehouse.execute(
                "INSERT INTO ops.ingestion_metadata (object_key,source_id,schema_version,subject_key,source_stream,logical_key,observed_at,content_sha256,byte_size,status,parser_version,started_at) VALUES (?, 'mac_screen_time', 2, ?, 'app-usage', ?, ?, ?, 1, 'succeeded', 'v1', ?)",
                [identity.object_key, device, segment, observed, digest, observed],
            )
    warehouse.execute(
        "INSERT INTO base.mac_screen_time_event (event_key,device_key,platform,source_stream,bundle_id,event_at,state,object_key,segment_key,observed_at,parser_version,unknown_field_count,duplicate_occurrence_count,is_active,loaded_at) VALUES ('event', ?, 'macos', 'app-usage', 'app', ?, 'start', ?, ?, ?, 'v1', 0, 1, ?, ?)",
        [device, observed, keys[0].object_key, keys[0].segment_key, observed, active, observed],
    )
    for n, segment in enumerate([keys[0].segment_key, keys[0].segment_key, keys[1].segment_key]):
        warehouse.execute(
            "INSERT INTO ops.mac_screen_time_record (physical_id,device_key,source_stream,segment_key,record_offset,record_metadata_offset,event_key,bundle_id,event_at,state,parser_version,unknown_field_count) VALUES (?,?,'app-usage',?,?,0,'event','app',?,'start','v1',0)",
            [f"p{n}", device, segment, n, observed],
        )
    migrate_state(warehouse, old, new, source_id="mac_screen_time", apply=True)
    with sqlite3.connect(new) as db:
        assert db.execute(
            "SELECT segment_key,event_key FROM event_reference ORDER BY 1"
        ).fetchall() == [(item.segment_key, "event") for item in keys]
        assert db.execute(
            "SELECT count(*) FROM file_state WHERE status='sent' AND parser_version='v1'"
        ).fetchone() == (2,)
    warehouse.execute(
        "UPDATE ops.mac_screen_time_record SET parser_version='v2' WHERE physical_id='p0'"
    )
    import pytest

    with pytest.raises(ValueError, match="parser"):
        migrate_state(
            warehouse,
            old,
            tmp_path / "other" / "collector.db",
            source_id="mac_screen_time",
            apply=False,
        )
    warehouse.execute(
        "UPDATE ops.mac_screen_time_record SET parser_version='v1' WHERE physical_id='p0'"
    )
    warehouse.execute(
        "UPDATE ops.mac_screen_time_record SET bundle_id='wrong' WHERE physical_id='p1'"
    )
    with pytest.raises(ValueError, match="inconsistent Screen Time reference"):
        migrate_state(
            warehouse,
            old,
            tmp_path / "mismatch" / "collector.db",
            source_id="mac_screen_time",
            apply=False,
        )
    assert not (tmp_path / "mismatch").exists()
    warehouse.close()


@pytest.mark.parametrize(
    ("source_id", "platform", "stream"),
    [("iphone_screen_time", "ios", "app-in-focus"), ("mac_screen_time", "macos", "app-usage")],
)
@pytest.mark.parametrize("previous_sent", [False, True])
def test_committed_pending_with_lost_ack_keeps_parser_and_rejects_different_replay(
    tmp_path, source_id, platform, stream, previous_sent
):
    from datetime import timedelta

    from personal_data_platform.biome.runtime import load_pending
    from personal_data_platform.sources.registry import get_source
    from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "out" / "collector.db"
    raw = b"pending"
    compressed = gzip.compress(raw, mtime=0)
    identity = ScreenTimeRawIdentity(
        source_id,
        "a" * 64,
        stream,
        "b" * 64,
        datetime(2026, 1, 1, tzinfo=UTC),
        hashlib.sha256(raw).hexdigest(),
    )
    snapshots = [(identity, "pending", "v1")]
    if previous_sent:
        previous = ScreenTimeRawIdentity(
            source_id,
            identity.device_key,
            stream,
            identity.segment_key,
            identity.observed_at - timedelta(seconds=1),
            identity.sha256,
        )
        snapshots.insert(0, (previous, "uploaded", "v0"))
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        for item, status, parser in snapshots:
            db.execute(
                "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status) VALUES (?,?,?,?,?,?,?,?)",
                (
                    item.device_key,
                    stream,
                    item.segment_key,
                    format_observed_at(item.observed_at),
                    item.sha256,
                    item.object_key,
                    compressed,
                    status,
                ),
            )
            warehouse.execute(
                "INSERT INTO ops.ingestion_metadata (object_key,source_id,schema_version,subject_key,source_stream,logical_key,observed_at,content_sha256,byte_size,status,parser_version,started_at) VALUES (?, ?, 2, ?, ?, ?, ?, ?, 7, 'succeeded', ?, ?)",
                [
                    item.object_key,
                    source_id,
                    item.device_key,
                    stream,
                    item.segment_key,
                    item.observed_at,
                    item.sha256,
                    parser,
                    item.observed_at,
                ],
            )
    warehouse.execute(
        f'INSERT INTO ops."{source_id}_segment" VALUES (?,?,?,?,?)',
        [
            identity.device_key,
            stream,
            identity.segment_key,
            identity.observed_at,
            identity.object_key,
        ],
    )
    warehouse.execute(
        f'INSERT INTO base."{source_id}_event" (event_key,device_key,platform,source_stream,bundle_id,event_at,state,object_key,segment_key,observed_at,parser_version,unknown_field_count,duplicate_occurrence_count,is_active,loaded_at) '
        "VALUES ('event',?,?,?,'app',?,'start',?,?,?,'v1',0,1,true,?)",
        [
            identity.device_key,
            platform,
            stream,
            identity.observed_at,
            identity.object_key,
            identity.segment_key,
            identity.observed_at,
            identity.observed_at,
        ],
    )
    warehouse.execute(
        f'INSERT INTO ops."{source_id}_record" (physical_id,device_key,source_stream,segment_key,record_offset,record_metadata_offset,event_key,bundle_id,event_at,state,parser_version,unknown_field_count) '
        "VALUES ('physical',?,?,?,1,2,'event','app',?,'start','v1',0)",
        [identity.device_key, stream, identity.segment_key, identity.observed_at],
    )
    before = old.read_bytes()
    migrate_state(warehouse, old, new, source_id=source_id, apply=True)
    state = CollectorState(new, source_id=source_id)
    pending = state.pending()[0]
    assert pending.identity == identity
    assert pending.compressed_payload == compressed
    assert pending.parser_version == "v1"
    with sqlite3.connect(new) as db:
        assert db.execute(
            "SELECT parser_version FROM file_state WHERE status='sent'"
        ).fetchall() == ([("v0",)] if previous_sent else [])
        assert db.execute("SELECT segment_key,event_key FROM event_reference").fetchall() == [
            (identity.segment_key, "event")
        ]
    stage = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        stage.migrate()
        stage.connection.execute(
            f"INSERT INTO base.{source_id}_event VALUES ('event',?,'app',?,'start',true)",
            [identity.device_key, identity.observed_at],
        )
        source = get_source(source_id)
        assert source.parser_version != "v1"
        with pytest.raises(RuntimeError, match="pending parser version"):
            load_pending(state, stage, source=source)
        assert state.pending()[0].parser_version == "v1"
        assert stage.connection.execute(
            f'SELECT is_valid FROM base."{source_id}_event"'
        ).fetchall() == [(True,)]
        with sqlite3.connect(new) as db:
            assert db.execute("SELECT count(*) FROM event_reference").fetchone() == (1,)
    finally:
        stage.close()
    assert old.read_bytes() == before
    warehouse.close()


def test_prepare_install_rebases_pending_paths_before_activation(tmp_path):
    from shutil import copyfile

    from scripts.migrate_analytics_schema import prepare_install

    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, staged = tmp_path / "old.db", tmp_path / "staged" / "collector.db"
    canonical = tmp_path / "live" / "collector.db"
    data = b"preserve"
    identity = ScreenTimeRawIdentity(
        "iphone_screen_time",
        "a" * 64,
        "app-in-focus",
        "b" * 64,
        datetime(2026, 1, 1, tzinfo=UTC),
        hashlib.sha256(data).hexdigest(),
    )
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        db.execute(
            "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status) VALUES (?,?,?,?,?,?,?,?)",
            (
                identity.device_key,
                identity.stream,
                identity.segment_key,
                format_observed_at(identity.observed_at),
                identity.sha256,
                identity.object_key,
                gzip.compress(data, mtime=0),
                "pending",
            ),
        )
    migrate_state(warehouse, old, staged, source_id="iphone_screen_time", apply=True)
    canonical.parent.mkdir()
    copyfile(old, canonical)
    before = canonical.read_bytes()
    ready = prepare_install(staged, canonical, source_id="iphone_screen_time", apply=False)
    assert not ready.exists()
    assert canonical.read_bytes() == before
    prepare_install(staged, canonical, source_id="iphone_screen_time", apply=True)
    assert canonical.read_bytes() == before
    canonical.rename(canonical.with_suffix(".old.db"))
    ready.rename(canonical)
    pending = CollectorState(canonical, source_id="iphone_screen_time").pending()
    assert pending[0].identity == identity
    assert gzip.decompress(pending[0].compressed_payload) == data
    assert staged.exists()
    warehouse.close()


def test_equal_observation_time_with_different_keys_is_rejected(tmp_path):
    import pytest

    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "new" / "collector.db"
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        for content in (b"a", b"b"):
            identity = ScreenTimeRawIdentity(
                "iphone_screen_time",
                "a" * 64,
                "app-in-focus",
                "b" * 64,
                observed,
                hashlib.sha256(content).hexdigest(),
            )
            db.execute(
                "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status) VALUES (?,?,?,?,?,?,?,?)",
                (
                    identity.device_key,
                    identity.stream,
                    identity.segment_key,
                    format_observed_at(observed),
                    identity.sha256,
                    identity.object_key,
                    gzip.compress(content, mtime=0),
                    "pending",
                ),
            )
    with pytest.raises(ValueError, match="observation time"):
        migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=True)
    assert not new.parent.exists()
    warehouse.close()


def test_interruption_does_not_publish_sqlite_and_retry_rejects_partial(tmp_path, monkeypatch):
    import os

    import pytest

    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "stage" / "collector.db"
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
    original = old.read_bytes()

    def fail_link(source, target):
        raise OSError("interrupted before publishing sqlite")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError):
        migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=True)
    assert old.read_bytes() == original
    assert not new.exists()
    assert new.with_suffix(".payloads").is_dir()
    with pytest.raises(ValueError, match="target"):
        migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=True)
    warehouse.close()


def test_invalid_legacy_generation_fails_before_creating_output(tmp_path):
    import pytest

    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "stage" / "collector.db"
    raw = b"x"
    identity = ScreenTimeRawIdentity(
        "iphone_screen_time",
        "a" * 64,
        "app-in-focus",
        "b" * 64,
        datetime(2026, 1, 1, tzinfo=UTC),
        hashlib.sha256(raw).hexdigest(),
    )
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        db.execute(
            "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status,storage_generation) VALUES (?,?,?,?,?,?,?,?,0)",
            (
                identity.device_key,
                identity.stream,
                identity.segment_key,
                format_observed_at(identity.observed_at),
                identity.sha256,
                identity.object_key,
                gzip.compress(raw, mtime=0),
                "pending",
            ),
        )
    with pytest.raises(ValueError, match="generation"):
        migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=True)
    assert not new.parent.exists()
    warehouse.close()


def test_uncheckpointed_legacy_sqlite_is_rejected_without_creating_files(tmp_path):
    import pytest

    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "stage" / "collector.db"
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
    wal = Path(str(old) + "-wal")
    wal.write_bytes(b"uncheckpointed")
    before = old.read_bytes()
    with pytest.raises(ValueError, match="journal"):
        migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=False)
    assert wal.read_bytes() == b"uncheckpointed"
    assert old.read_bytes() == before
    assert not new.parent.exists()
    warehouse.close()


@pytest.mark.parametrize(
    ("source_id", "platform", "stream"),
    [("iphone_screen_time", "ios", "app-in-focus"), ("mac_screen_time", "macos", "app-usage")],
)
@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("active", [False, True])
def test_unreferenced_event_only_permitted_when_inactive(
    tmp_path, source_id, platform, stream, apply, active
):
    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    warehouse.execute(
        f'INSERT INTO base."{source_id}_event" (event_key,device_key,platform,source_stream,bundle_id,event_at,state,object_key,segment_key,observed_at,parser_version,unknown_field_count,duplicate_occurrence_count,is_active,loaded_at) '
        "VALUES ('orphan','device',?,?, 'app',current_timestamp,'start','object','segment',current_timestamp,'v1',0,0,?,current_timestamp)",
        [platform, stream, active],
    )
    old, target = tmp_path / "old.db", tmp_path / "prepared" / "collector.db"
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
    source_before = old.read_bytes()
    if active:
        with pytest.raises(ValueError, match="active.*reference"):
            migrate_state(warehouse, old, target, source_id=source_id, apply=apply)
        assert not target.parent.exists()
    else:
        migrate_state(warehouse, old, target, source_id=source_id, apply=apply)
        assert target.exists() is apply
    assert old.read_bytes() == source_before
    warehouse.close()


@pytest.mark.parametrize(
    ("source_id", "stream"),
    [("iphone_screen_time", "app-in-focus"), ("mac_screen_time", "app-usage")],
)
@pytest.mark.parametrize("case", ["tombstones", "events", "corrupt", "bad-receipt"])
@pytest.mark.parametrize("apply", [False, True])
def test_sent_tombstone_without_warehouse_segment_requires_payload_and_receipt(
    tmp_path, source_id, stream, case, apply
):
    from personal_data_platform.biome.raw import encode_segment_envelope

    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "prepared" / "collector.db"
    data = encode_segment_envelope(
        b"ignored deletion notice", kind="events" if case == "events" else "tombstones"
    )
    identity = ScreenTimeRawIdentity(
        source_id,
        "a" * 64,
        stream,
        "b" * 64,
        datetime(2026, 1, 1, tzinfo=UTC),
        hashlib.sha256(data).hexdigest(),
    )
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        db.execute(
            "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status) VALUES (?,?,?,?,?,?,?,'uploaded')",
            (
                identity.device_key,
                stream,
                identity.segment_key,
                format_observed_at(identity.observed_at),
                identity.sha256,
                identity.object_key,
                gzip.compress(b"wrong" if case == "corrupt" else data, mtime=0),
            ),
        )
    warehouse.execute(
        "INSERT INTO ops.ingestion_metadata (object_key,source_id,schema_version,subject_key,source_stream,logical_key,observed_at,content_sha256,byte_size,status,parser_version,started_at) VALUES (?, ?, 2, ?, ?, ?, ?, ?, ?, ?, 'v1', ?)",
        [
            identity.object_key,
            source_id,
            identity.device_key,
            stream,
            identity.segment_key,
            identity.observed_at,
            identity.sha256,
            len(data),
            "failed" if case == "bad-receipt" else "succeeded",
            identity.observed_at,
        ],
    )
    before = old.read_bytes()
    if case != "tombstones":
        with pytest.raises(ValueError):
            migrate_state(warehouse, old, new, source_id=source_id, apply=apply)
        assert not new.parent.exists()
    else:
        migrate_state(warehouse, old, new, source_id=source_id, apply=apply)
        if apply:
            with sqlite3.connect(new) as db:
                assert db.execute(
                    "SELECT status,parser_version,payload_path FROM file_state"
                ).fetchall() == [("sent", "v1", None)]
                assert db.execute("SELECT count(*) FROM event_reference").fetchone() == (0,)
            assert not CollectorState(new, source_id=source_id).pending()
        else:
            assert not new.parent.exists()
    assert old.read_bytes() == before
    warehouse.close()

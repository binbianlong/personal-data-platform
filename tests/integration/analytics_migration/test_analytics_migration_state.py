import gzip
import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import duckdb

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
    with sqlite3.connect(new) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
    warehouse.close()


def test_old_pending_before_sent_rejected_before_output(tmp_path):
    from datetime import timedelta

    import pytest

    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "new" / "collector.db"
    device, segment = "a" * 64, "b" * 64
    first = datetime(2026, 1, 1, tzinfo=UTC)
    data = b"content"
    digest = hashlib.sha256(data).hexdigest()
    pending = ScreenTimeRawIdentity(
        "iphone_screen_time", device, "app-in-focus", segment, first, digest
    )
    sent = ScreenTimeRawIdentity(
        "iphone_screen_time", device, "app-in-focus", segment, first + timedelta(seconds=1), digest
    )
    with sqlite3.connect(old) as db:
        db.executescript(LEGACY_SQLITE_SCHEMA)
        for item, status in [(pending, "pending"), (sent, "uploaded")]:
            db.execute(
                "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status) VALUES (?,?,?,?,?,?,?,?)",
                (
                    device,
                    item.stream,
                    segment,
                    format_observed_at(item.observed_at),
                    digest,
                    item.object_key,
                    gzip.compress(data, mtime=0),
                    status,
                ),
            )
    warehouse.execute(
        "INSERT INTO ops.iphone_screen_time_segment VALUES (?, ?, ?, ?, ?)",
        [device, sent.stream, segment, sent.observed_at, sent.object_key],
    )
    warehouse.execute(
        "INSERT INTO ops.ingestion_metadata (object_key,source_id,schema_version,subject_key,source_stream,logical_key,observed_at,content_sha256,byte_size,status,parser_version,started_at) VALUES (?, 'iphone_screen_time', 2, ?, 'app-in-focus', ?, ?, ?, 7, 'succeeded', 'v1', ?)",
        [sent.object_key, device, segment, sent.observed_at, digest, sent.observed_at],
    )
    with pytest.raises(ValueError, match="pending snapshot"):
        migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=True)
    assert not new.parent.exists()
    warehouse.close()


def test_distinct_refs_preserve_multiple_segments_and_inactive_event(tmp_path):
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
        "INSERT INTO base.iphone_screen_time_event (event_key,device_key,platform,source_stream,bundle_id,event_at,state,object_key,segment_key,observed_at,parser_version,unknown_field_count,duplicate_occurrence_count,is_active,loaded_at) VALUES ('event', ?, 'ios', 'app-in-focus', 'app', ?, 'start', ?, ?, ?, 'v1', 0, 1, false, ?)",
        [device, observed, keys[0].object_key, keys[0].segment_key, observed, observed],
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
    warehouse.close()


def test_mac_distinct_refs_preserve_multiple_segments_and_inactive_event(tmp_path):
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
        "INSERT INTO base.mac_screen_time_event (event_key,device_key,platform,source_stream,bundle_id,event_at,state,object_key,segment_key,observed_at,parser_version,unknown_field_count,duplicate_occurrence_count,is_active,loaded_at) VALUES ('event', ?, 'macos', 'app-usage', 'app', ?, 'start', ?, ?, ?, 'v1', 0, 1, false, ?)",
        [device, observed, keys[0].object_key, keys[0].segment_key, observed, observed],
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
    warehouse.close()


def test_committed_pending_with_lost_ack_is_kept_pending(tmp_path):
    warehouse = duckdb.connect()
    warehouse.execute(Path("tests/fixtures/analytics_migration/legacy_schema.sql").read_text())
    old, new = tmp_path / "old.db", tmp_path / "out" / "collector.db"
    raw = b"pending"
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
            "INSERT INTO segment_observation (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,status) VALUES (?,?,?,?,?,?,?,?)",
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
    warehouse.execute(
        "INSERT INTO ops.iphone_screen_time_segment VALUES (?,?,?,?,?)",
        [
            identity.device_key,
            identity.stream,
            identity.segment_key,
            identity.observed_at,
            identity.object_key,
        ],
    )
    warehouse.execute(
        "INSERT INTO ops.ingestion_metadata (object_key,source_id,schema_version,subject_key,source_stream,logical_key,observed_at,content_sha256,byte_size,status,parser_version,started_at) VALUES (?, 'iphone_screen_time', 2, ?, 'app-in-focus', ?, ?, ?, 7, 'succeeded', 'v1', ?)",
        [
            identity.object_key,
            identity.device_key,
            identity.segment_key,
            identity.observed_at,
            identity.sha256,
            identity.observed_at,
        ],
    )
    migrate_state(warehouse, old, new, source_id="iphone_screen_time", apply=True)
    assert CollectorState(new, source_id="iphone_screen_time").pending()[0].identity == identity
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

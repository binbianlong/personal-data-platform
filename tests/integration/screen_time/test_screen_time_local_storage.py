import gzip
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.collector import (
    BiomeScreenTimeSource,
    ScreenTimeCollector,
)
from personal_data_platform.sources.screen_time.raw import (
    ScreenTimeRawIdentity,
    build_device_key,
    encode_segment_envelope,
    format_observed_at,
)
from personal_data_platform.sources.screen_time.state import CollectorScan, CollectorState
from personal_data_platform.sources.screen_time.storage import ScreenTimeLocalRepository
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import event, segb

NOW = datetime(2026, 10, 8, tzinfo=UTC)


@pytest.mark.parametrize("stream", ["app-in-focus", "app-usage"])
def test_local_scan_records_latest_devices_and_explicit_inactivity(tmp_path, stream):
    state = CollectorState(tmp_path / "collector.db")
    repo = ScreenTimeLocalRepository(state=state, source=get_source("screen_time", stream))
    assert repo.last_successful_scan() is None
    state.record_successful_scan(CollectorScan(NOW, ("a" * 64,), {"a" * 64: NOW}), stream=stream)
    latest = NOW + timedelta(hours=24)
    state.record_successful_scan(CollectorScan(latest, ("a" * 64,), {}), stream=stream)
    restarted = ScreenTimeLocalRepository(
        state=CollectorState(state.path), source=get_source("screen_time", stream)
    )
    assert restarted.last_successful_scan() == CollectorScan(latest, ("a" * 64,), {"a" * 64: NOW})
    state.record_successful_scan(CollectorScan(latest, (), {}), stream=stream)
    assert restarted.last_successful_scan() == CollectorScan(latest, (), {})


@pytest.mark.parametrize("stream", ["app-in-focus", "app-usage"])
def test_legacy_retention_upgrade_preserves_raw_pending_and_scan(tmp_path, stream):
    state = CollectorState(tmp_path / "legacy.db")
    args = dict(device_key="a" * 64, stream=stream, segment_key="b" * 64)
    loaded = state.prepare(**args, raw_bytes=b"retained", observed_at=NOW)
    state.mark_loaded(loaded.identity.object_key, NOW)
    pending = state.prepare(**args, raw_bytes=b"pending", observed_at=NOW + timedelta(seconds=1))
    scan = CollectorScan(NOW, ("a" * 64,), {"a" * 64: NOW})
    state.record_successful_scan(scan, stream=stream)
    projection = (
        "id,device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,"
        "status,uploaded_at,storage_created_at,storage_generation"
    )
    with state._connect() as connection:
        if "retention_started_at" not in {
            r[1] for r in connection.execute("PRAGMA table_info(segment_observation)")
        }:
            connection.execute(
                "ALTER TABLE segment_observation ADD COLUMN retention_started_at TEXT"
            )
        connection.execute("UPDATE segment_observation SET retention_started_at=observed_at")
        before = connection.execute(
            f"SELECT {projection} FROM segment_observation ORDER BY id"
        ).fetchall()

    restarted = CollectorState(state.path)

    with restarted._connect() as connection:
        assert "retention_started_at" not in {
            r[1] for r in connection.execute("PRAGMA table_info(segment_observation)")
        }
        assert (
            connection.execute(
                f"SELECT {projection} FROM segment_observation ORDER BY id"
            ).fetchall()
            == before
        )
    assert restarted.last_successful_scan(stream=stream) == scan
    assert [p.identity for p in restarted.pending()] == [pending.identity]
    repository = ScreenTimeLocalRepository(
        state=restarted, source=get_source("screen_time", stream)
    )
    assert (
        gzip.decompress(repository.get_raw(loaded.identity.object_key, generation=1)) == b"retained"
    )
    assert (
        gzip.decompress(repository.get_raw(pending.identity.object_key, generation=1)) == b"pending"
    )
    restarted.mark_loaded(pending.identity.object_key, NOW)
    assert restarted.pending() == []
    assert [r.key for r in repository.list_raw(f"raw/screen_time/v1/{'a' * 64}/{stream}/")] == [
        pending.identity.object_key
    ]


def test_loaded_raw_survives_restart_and_only_superseded_success_is_removed(tmp_path):
    state = CollectorState(tmp_path / "collector.db")
    args = dict(device_key="a" * 64, stream="app-in-focus", segment_key="b" * 64)
    first = state.prepare(**args, raw_bytes=b"first", observed_at=NOW)
    state.mark_loaded(first.identity.object_key, NOW)
    second = state.prepare(**args, raw_bytes=b"second", observed_at=NOW + timedelta(seconds=1))
    third = state.prepare(**args, raw_bytes=b"third", observed_at=NOW + timedelta(seconds=2))
    repo = ScreenTimeLocalRepository(state=state, source=get_source())
    assert gzip.decompress(repo.get_raw(first.identity.object_key, generation=1)) == b"first"
    assert len(list(repo.list_raw("raw/screen_time/v1/"))) == 3
    state.mark_loaded(second.identity.object_key, NOW)
    assert {r.key for r in repo.list_raw("raw/screen_time/v1/")} == {
        second.identity.object_key,
        third.identity.object_key,
    }
    state.mark_loaded(third.identity.object_key, NOW)
    storage_created_at = NOW - timedelta(days=2)
    with state._connect() as connection:
        connection.execute(
            """UPDATE segment_observation SET storage_created_at = ?, storage_generation = 7
                      WHERE object_key = ?""",
            (
                format_observed_at(storage_created_at),
                third.identity.object_key,
            ),
        )
    restarted = ScreenTimeLocalRepository(state=CollectorState(state.path), source=get_source())
    retained = list(restarted.list_raw("raw/screen_time/v1/"))
    assert len(retained) == 1
    assert retained[0].key == third.identity.object_key
    assert retained[0].storage_created_at == storage_created_at
    assert retained[0].storage_generation == 7
    assert gzip.decompress(restarted.get_raw(third.identity.object_key, generation=7)) == b"third"
    with pytest.raises(ValueError, match="generation"):
        restarted.get_raw(third.identity.object_key, generation=2)


def test_latest_segment_is_saved_pending_without_successor(tmp_path):
    sync = tmp_path / "sync.db"
    with sqlite3.connect(sync) as c:
        c.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT)"
        )
        c.execute("INSERT INTO DevicePeer VALUES ('phone', 'Phone', 'P', 2)")
    remote = tmp_path / "remote" / "phone"
    remote.mkdir(parents=True)
    body, _ = segb(event("synthetic.app"))
    (remote / "100").write_bytes(body)
    state = CollectorState(tmp_path / "collector.db")
    collector = ScreenTimeCollector(
        source=BiomeScreenTimeSource(sync_db_path=sync, remote_dir=remote.parent),
        state=state,
        pseudonym_key=b"x" * 32,
        allowed_device_keys=frozenset({build_device_key(b"x" * 32, "phone")}),
        clock=lambda: NOW,
    )
    assert collector.collect_once().deferred == 0
    pending = state.pending()
    assert len(pending) == 1
    assert gzip.decompress(pending[0].compressed_payload) == encode_segment_envelope(
        body, name="100", kind="events"
    )
    (remote / "100").write_bytes(segb(event("synthetic.other"), crc=0)[0])
    assert collector.collect_once().deferred == 1
    assert [p.identity.object_key for p in state.pending()] == [pending[0].identity.object_key]


@pytest.mark.parametrize("publish_success", [True, False])
def test_warehouse_commit_then_restart_acknowledges_without_duplicate_events(
    tmp_path, publish_success
):
    from personal_data_platform.sources.screen_time.runtime import load_pending

    state = CollectorState(tmp_path / "collector.db")
    body = encode_segment_envelope(segb(event("synthetic.app"))[0], name="100", kind="events")
    pending = state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        raw_bytes=body,
        observed_at=NOW,
        schema_version=2,
    )
    source = get_source()
    repo = ScreenTimeLocalRepository(state=state, source=source)
    state.record_successful_scan(CollectorScan(NOW, ("a" * 64,), {"a" * 64: NOW}))
    state.record_successful_scan(CollectorScan(NOW, (), {}), stream="app-usage")
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    from personal_data_platform.loader.job import run_loader_objects

    refs = tuple(repo.list_raw("raw/screen_time/v2/"))
    assert run_loader_objects(repo, wh, refs, source=source).ok
    assert state.pending()  # Process died before SQLite acknowledgement.
    load_pending(state, wh, now=NOW, publish_success=publish_success)
    assert not state.pending()
    assert wh.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    assert wh.query_value("SELECT count(*) FROM ops.heartbeat") == (2 if publish_success else 0)
    assert repo.get_raw(pending.identity.object_key, generation=1) == pending.compressed_payload
    successor = state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        raw_bytes=encode_segment_envelope(
            segb(event("synthetic.other"))[0], name="100", kind="events"
        ),
        observed_at=NOW + timedelta(seconds=1),
        schema_version=2,
    )
    load_pending(state, wh, now=NOW + timedelta(seconds=1), publish_success=publish_success)
    assert wh.query_rows("SELECT object_key FROM ops.ingestion_metadata") == [
        (successor.identity.object_key,)
    ]
    assert {raw.key for raw in repo.list_raw("raw/screen_time/v2/")} == {
        successor.identity.object_key
    }
    wh.close()


def test_mac_audit_requires_its_own_device_scan_even_when_iphone_is_fresh(tmp_path):
    from personal_data_platform.sources.screen_time.audit import audit_source

    state = CollectorState(tmp_path / "collector.db")
    state.record_successful_scan(CollectorScan(NOW, ("a" * 64,), {"a" * 64: NOW}))
    repository = ScreenTimeLocalRepository(
        state=state, source=get_source("screen_time", "app-usage")
    )
    state.record_successful_scan(CollectorScan(NOW, ("b" * 64,), {}), stream="app-usage")
    health = audit_source(repository, [], NOW)
    assert not health.ok and health.details["missing_collector_scan_count"] == 1
    state.record_successful_scan(
        CollectorScan(NOW, ("b" * 64,), {"b" * 64: NOW}), stream="app-usage"
    )
    assert audit_source(repository, [], NOW).ok


def test_iphone_requires_scan_until_explicitly_marked_inactive(tmp_path) -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    state = CollectorState(tmp_path / "collector.db")
    source = get_source("screen_time", "app-in-focus")
    repository = ScreenTimeLocalRepository(state=state, source=source)
    assert not source.audit(repository, [], now).ok

    state.record_successful_scan(CollectorScan(now - timedelta(days=2), (), {}))
    health = source.audit(repository, [], now)
    assert health.ok
    assert health.details["collector_inactive"] is True


def test_mac_raw_without_scan_is_not_treated_as_never_activated(tmp_path) -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    source = get_source("screen_time", "app-usage")
    repository = ScreenTimeLocalRepository(
        state=CollectorState(tmp_path / "collector.db"), source=source
    )
    raw_key = ScreenTimeRawIdentity(
        device_key="b" * 64,
        stream="app-usage",
        segment_key="c" * 64,
        observed_at=now,
        sha256="d" * 64,
        schema_version=2,
    ).object_key
    raw = source.parse_raw_key(raw_key, storage_created_at=now, storage_generation=1)
    assert not source.audit(repository, [raw], now).ok

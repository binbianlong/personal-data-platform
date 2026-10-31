import gzip
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.biome.collector import ScreenTimeCollector
from personal_data_platform.biome.collector_state import CollectorScan, CollectorState
from personal_data_platform.sources.iphone_screen_time.collector import (
    BiomeScreenTimeSource,
)
from personal_data_platform.sources.iphone_screen_time.raw import (
    build_device_key,
    encode_segment_envelope,
    format_observed_at,
)
from personal_data_platform.sources.iphone_screen_time.storage import LocalRepository
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import event, segb

NOW = datetime(2026, 10, 8, tzinfo=UTC)


@pytest.mark.parametrize("stream", ["app-in-focus"])
def test_local_scan_records_latest_devices_and_explicit_inactivity(tmp_path, stream):
    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    repo = LocalRepository(state=state, source=get_source("iphone_screen_time", stream))
    assert repo.last_successful_scan() is None
    state.record_successful_scan(CollectorScan(NOW, ("a" * 64,), {"a" * 64: NOW}), stream=stream)
    latest = NOW + timedelta(hours=24)
    state.record_successful_scan(CollectorScan(latest, ("a" * 64,), {}), stream=stream)
    restarted = LocalRepository(
        state=CollectorState(state.path, source_id="iphone_screen_time"),
        source=get_source("iphone_screen_time", stream),
    )
    assert restarted.last_successful_scan() == CollectorScan(latest, ("a" * 64,), {"a" * 64: NOW})
    state.record_successful_scan(CollectorScan(latest, (), {}), stream=stream)
    assert restarted.last_successful_scan() == CollectorScan(latest, (), {})


def test_loaded_raw_survives_restart_and_only_superseded_success_is_removed(tmp_path):
    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    args = dict(device_key="a" * 64, stream="app-in-focus", segment_key="b" * 64)
    first = state.prepare(**args, raw_bytes=b"first", observed_at=NOW)
    state.mark_loaded(first.identity.object_key, NOW)
    second = state.prepare(**args, raw_bytes=b"second", observed_at=NOW + timedelta(seconds=1))
    third = state.prepare(**args, raw_bytes=b"third", observed_at=NOW + timedelta(seconds=2))
    repo = LocalRepository(state=state, source=get_source())
    assert gzip.decompress(repo.get_raw(first.identity.object_key, generation=1)) == b"first"
    assert len(list(repo.list_raw("raw/iphone_screen_time/v2/"))) == 3
    state.mark_loaded(second.identity.object_key, NOW)
    assert {r.key for r in repo.list_raw("raw/iphone_screen_time/v2/")} == {
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
    restarted = LocalRepository(
        state=CollectorState(state.path, source_id="iphone_screen_time"), source=get_source()
    )
    retained = list(restarted.list_raw("raw/iphone_screen_time/v2/"))
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
    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
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
    assert gzip.decompress(pending[0].compressed_payload) == encode_segment_envelope(body)
    (remote / "100").write_bytes(segb(event("synthetic.other"), crc=0)[0])
    assert collector.collect_once().deferred == 1
    assert [p.identity.object_key for p in state.pending()] == [pending[0].identity.object_key]


@pytest.mark.parametrize("publish_success", [True, False])
def test_warehouse_commit_then_restart_acknowledges_without_duplicate_events(
    tmp_path, publish_success
):
    from personal_data_platform.sources.iphone_screen_time.runtime import load_pending

    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    body = encode_segment_envelope(segb(event("synthetic.app"))[0])
    pending = state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        raw_bytes=body,
        observed_at=NOW,
    )
    source = get_source()
    repo = LocalRepository(state=state, source=source)
    state.record_successful_scan(
        CollectorScan(NOW, ("a" * 64,), {"a" * 64: NOW}), stream="app-in-focus"
    )
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    from personal_data_platform.loader.job import run_loader_objects

    refs = tuple(repo.list_raw("raw/iphone_screen_time/v2/"))
    assert run_loader_objects(repo, wh, refs, source=source).ok
    assert state.pending()  # Process died before SQLite acknowledgement.
    load_pending(state, wh, now=NOW, publish_success=publish_success)
    assert not state.pending()
    assert wh.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 1
    assert wh.query_value("SELECT count(*) FROM ops.heartbeat") == (1 if publish_success else 0)
    assert repo.get_raw(pending.identity.object_key, generation=1) == pending.compressed_payload
    successor = state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        raw_bytes=encode_segment_envelope(segb(event("synthetic.other"))[0]),
        observed_at=NOW + timedelta(seconds=1),
    )
    load_pending(state, wh, now=NOW + timedelta(seconds=1), publish_success=publish_success)
    assert wh.query_rows("SELECT object_key FROM ops.ingestion_metadata") == [
        (successor.identity.object_key,)
    ]
    assert {raw.key for raw in repo.list_raw("raw/iphone_screen_time/v2/")} == {
        successor.identity.object_key
    }
    wh.close()


def test_iphone_requires_scan_until_explicitly_marked_inactive(tmp_path) -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    source = get_source("iphone_screen_time", "app-in-focus")
    repository = LocalRepository(state=state, source=source)
    assert not source.audit(repository, [], now).ok

    state.record_successful_scan(
        CollectorScan(now - timedelta(days=2), (), {}), stream="app-in-focus"
    )
    health = source.audit(repository, [], now)
    assert health.ok
    assert health.details["collector_inactive"] is True

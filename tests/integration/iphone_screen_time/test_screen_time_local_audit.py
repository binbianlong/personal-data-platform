import json
from datetime import UTC, datetime, timedelta

from personal_data_platform.biome.collector_state import CollectorScan, CollectorState
from personal_data_platform.sources.iphone_screen_time.raw import encode_segment_envelope
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import event, segb

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def test_local_audit_accepts_compacted_history_but_requires_current_raw(tmp_path):
    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.reconciliation.job import run_reconciliation
    from personal_data_platform.sources.iphone_screen_time.storage import LocalRepository

    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    source = get_source()
    repo = LocalRepository(state=state, source=source)
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    for relation in source.required_relations[1:]:
        wh.connection.execute(f"CREATE OR REPLACE VIEW {relation} AS SELECT 1 AS value")
    state.record_successful_scan(
        CollectorScan(NOW, ("a" * 64,), {"a" * 64: NOW}), stream="app-in-focus"
    )
    for index in range(2):
        pending = state.prepare(
            device_key="a" * 64,
            stream=source.stream,
            segment_key="b" * 64,
            raw_bytes=encode_segment_envelope(segb(event(f"synthetic.{index}"))[0]),
            observed_at=NOW + timedelta(seconds=index),
        )
        assert run_loader_objects(
            repo, wh, tuple(repo.list_raw("raw/iphone_screen_time/v2/")), source=source
        ).ok
        state.mark_loaded(pending.identity.object_key, NOW)
    assert run_reconciliation(repo, wh, source=source, now=NOW, repair_missing=False).ok
    with state._connect() as db:
        db.execute("DELETE FROM segment_observation")
    assert not run_reconciliation(repo, wh, source=source, now=NOW, repair_missing=False).ok
    wh.close()


def test_cloud_audit_uses_mac_heartbeat_and_rejects_stale_capture():
    from personal_data_platform.biome.audit import audit_collector_heartbeat

    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    source = get_source()
    now = datetime.now(UTC)
    missing = audit_collector_heartbeat(wh, source, now)
    assert not missing.ok and missing.details["collector_heartbeat_missing"]
    assert missing.details["checked_at"] == now.isoformat()
    wh.publish_heartbeat(source.monitor_name, "mac", {"scan_completed_at": now.isoformat()})
    assert audit_collector_heartbeat(wh, source, now).ok
    wh.publish_heartbeat(
        source.monitor_name, "mac", {"scan_completed_at": (now - timedelta(hours=49)).isoformat()}
    )
    stale = audit_collector_heartbeat(wh, source, now)
    assert not stale.ok
    assert stale.details["checked_at"] == now.isoformat()
    assert stale.details["scan_completed_at"] == (now - timedelta(hours=49)).isoformat()
    assert stale.details["collector_succeeded_at"] is not None
    wh.publish_heartbeat(
        source.monitor_name,
        "mac",
        {"collector_inactive": True, "scan_completed_at": now.isoformat()},
    )
    assert audit_collector_heartbeat(wh, source, now).ok
    assert not audit_collector_heartbeat(wh, source, now + timedelta(hours=49)).ok
    wh.close()


def test_cloud_audit_does_not_include_invalid_heartbeat_contents_in_diagnostics():
    from personal_data_platform.biome.audit import audit_collector_heartbeat

    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    source = get_source()
    now = datetime.now(UTC)
    try:
        wh.publish_heartbeat(
            source.monitor_name,
            "mac",
            {"scan_completed_at": "synthetic-token", "private": "synthetic-health"},
        )
        health = audit_collector_heartbeat(wh, source, now)
        assert not health.ok and health.details["collector_heartbeat_invalid"]
        assert health.details["checked_at"] == now.isoformat()
        logged = json.dumps(health.details)
        assert "synthetic-token" not in logged
        assert "synthetic-health" not in logged
    finally:
        wh.close()

from datetime import UTC, datetime, timedelta

from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.raw import encode_segment_envelope
from personal_data_platform.sources.screen_time.state import CollectorState
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import event, segb

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def test_local_audit_accepts_compacted_history_but_requires_current_raw(tmp_path):
    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.reconciliation.job import run_reconciliation
    from personal_data_platform.sources.screen_time.raw import (
        CollectorDeviceManifest,
        CollectorScanReceipt,
    )
    from personal_data_platform.sources.screen_time.storage import ScreenTimeLocalRepository

    state = CollectorState(tmp_path / "collector.db")
    source = get_source()
    repo = ScreenTimeLocalRepository(state=state, source=source)
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    for relation in source.required_relations[1:]:
        wh.connection.execute(f"CREATE OR REPLACE VIEW {relation} AS SELECT 1 AS value")
    repo.put_device_manifest(CollectorDeviceManifest(device_keys=("a" * 64,), completed_at=NOW))
    repo.put_scan_receipt(
        CollectorScanReceipt(device_key="a" * 64, completed_at=NOW, segment_count=1)
    )
    for index in range(2):
        pending = state.prepare(
            device_key="a" * 64,
            stream=source.stream,
            segment_key="b" * 64,
            raw_bytes=encode_segment_envelope(
                segb(event(f"synthetic.{index}"))[0], name="100", kind="events"
            ),
            observed_at=NOW + timedelta(seconds=index),
            schema_version=2,
        )
        assert run_loader_objects(
            repo, wh, tuple(repo.list_raw("raw/screen_time/v2/")), source=source
        ).ok
        state.mark_uploaded(pending.identity.object_key, NOW)
    assert run_reconciliation(
        repo, wh, source=source, heartbeat=lambda _: None, now=NOW, repair_missing=False
    ).ok
    with state._connect() as db:
        db.execute("DELETE FROM segment_observation")
    assert not run_reconciliation(
        repo, wh, source=source, heartbeat=lambda _: None, now=NOW, repair_missing=False
    ).ok
    wh.close()


def test_cloud_audit_uses_mac_heartbeat_and_rejects_stale_capture():
    from personal_data_platform.reconciliation.job import _audit_screen_time_heartbeat

    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    source = get_source()
    now = datetime.now(UTC)
    assert not _audit_screen_time_heartbeat(wh, source, now).ok
    wh.publish_heartbeat(source.monitor_name, "mac", {"scan_completed_at": now.isoformat()})
    assert _audit_screen_time_heartbeat(wh, source, now).ok
    wh.publish_heartbeat(
        source.monitor_name, "mac", {"scan_completed_at": (now - timedelta(hours=49)).isoformat()}
    )
    assert not _audit_screen_time_heartbeat(wh, source, now).ok
    wh.publish_heartbeat(
        source.monitor_name,
        "mac",
        {"collector_inactive": True, "scan_completed_at": now.isoformat()},
    )
    assert _audit_screen_time_heartbeat(wh, source, now).ok
    assert not _audit_screen_time_heartbeat(wh, source, now + timedelta(hours=49)).ok
    wh.close()

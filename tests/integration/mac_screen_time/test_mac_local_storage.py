from datetime import UTC, datetime

from personal_data_platform.biome.collector_state import CollectorScan, CollectorState
from personal_data_platform.sources.mac_screen_time.storage import LocalRepository
from personal_data_platform.sources.registry import get_source

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def test_mac_audit_requires_its_own_device_scan_even_when_iphone_is_fresh(tmp_path):
    from personal_data_platform.biome.audit import audit_source

    state = CollectorState(tmp_path / "collector.db", source_id="mac_screen_time")
    repository = LocalRepository(state=state, source=get_source("mac_screen_time", "app-usage"))
    state.record_successful_scan(CollectorScan(NOW, ("b" * 64,), {}), stream="app-usage")
    health = audit_source(repository, [], NOW)
    assert not health.ok and health.details["missing_collector_scan_count"] == 1
    state.record_successful_scan(
        CollectorScan(NOW, ("b" * 64,), {"b" * 64: NOW}), stream="app-usage"
    )
    assert audit_source(repository, [], NOW).ok


def test_mac_raw_without_scan_is_not_treated_as_never_activated(tmp_path) -> None:
    from personal_data_platform.sources.mac_screen_time.raw import ScreenTimeRawIdentity

    now = datetime(2026, 9, 26, tzinfo=UTC)
    source = get_source("mac_screen_time", "app-usage")
    repository = LocalRepository(
        state=CollectorState(tmp_path / "collector.db", source_id="mac_screen_time"), source=source
    )
    raw_key = ScreenTimeRawIdentity(
        device_key="b" * 64,
        stream="app-usage",
        segment_key="c" * 64,
        observed_at=now,
        sha256="d" * 64,
    ).object_key
    raw = source.parse_raw_key(raw_key, storage_created_at=now, storage_generation=1)
    assert not source.audit(repository, [raw], now).ok

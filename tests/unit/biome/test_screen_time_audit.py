from personal_data_platform.biome.audit import audit_source
from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.biome.storage import BiomeLocalRepository
from personal_data_platform.sources.iphone_screen_time.adapter import IPhoneScreenTimeSource
from tests.screen_time_helpers import NOW


def test_local_health_checks_pending_bytes_without_claiming_collector_liveness(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    repository = BiomeLocalRepository(state=state, source=IPhoneScreenTimeSource())
    health = audit_source(repository, [], NOW)
    assert health.ok
    assert health.details == {"pending_snapshot_count": 0, "collector_liveness": "local_logs"}
    state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        raw_bytes=b"bytes",
        observed_at=NOW,
    )
    health = audit_source(repository, [], NOW)
    assert not health.ok
    assert health.details["pending_snapshot_count"] == 1

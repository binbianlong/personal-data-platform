"""Mac pending state cannot consume the iPhone namespace."""

import pytest

from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.biome.raw import encode_segment_envelope
from personal_data_platform.sources.iphone_screen_time.adapter import IPhoneScreenTimeSource
from personal_data_platform.sources.mac_screen_time.adapter import MacScreenTimeSource
from personal_data_platform.sources.mac_screen_time.storage import LocalRepository
from tests.screen_time_helpers import NOW, mac_usage_event, segb


def test_mac_repository_rejects_counterpart_state(tmp_path):
    state = CollectorState(tmp_path / "phone.db", source_id="iphone_screen_time")
    with pytest.raises(ValueError, match="different source"):
        LocalRepository(state=state, source=MacScreenTimeSource())


def test_mac_payload_and_state_are_independent(tmp_path):
    state = CollectorState(tmp_path / "mac.db", source_id="mac_screen_time")
    pending = state.prepare(
        device_key="b" * 64,
        stream="app-usage",
        segment_key="c" * 64,
        raw_bytes=encode_segment_envelope(segb(mac_usage_event("mac.app", 1000.25, start=True))[0]),
        observed_at=NOW,
    )
    repository = LocalRepository(state=state, source=MacScreenTimeSource())
    assert [raw.key for raw in repository.list_raw("raw/mac_screen_time/")] == [
        pending.identity.object_key
    ]
    assert list(repository.list_raw("raw/iphone_screen_time/")) == []
    with pytest.raises(ValueError, match="different source"):
        LocalRepository(state=state, source=IPhoneScreenTimeSource())

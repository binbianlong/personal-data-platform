"""The small device-sync control object survives receipt lifecycle deletion."""

from dataclasses import replace
from datetime import date

import pytest
from google.api_core.exceptions import PreconditionFailed

from personal_data_platform.sources.fitbit.api import SyncTime
from personal_data_platform.sources.fitbit.sync_state import GCSFitbitSyncState, SyncState
from tests.unit.test_fitbit_receipts import Client


def test_sync_control_uses_generation_guard_and_retains_nanosecond_cursor():
    client = Client()
    store = GCSFitbitSyncState(client=client, bucket="test")
    initial = store.read("self")
    assert initial.generation == 0
    state = replace(
        initial.state,
        bootstrap_day=date(2026, 9, 28),
        bootstrap_complete=True,
        last_completed_sync=SyncTime.parse("2026-09-28T01:02:03.123456789Z"),
    )
    saved = store.replace(initial, state)
    assert store.read("self") == saved
    assert saved.state.last_completed_sync.text == "2026-09-28T01:02:03.123456789Z"
    with pytest.raises(PreconditionFailed):
        store.replace(initial, state)
    assert list(client.values) == ["raw/fitbit/v1/_control/device-sync/self.json"]


def test_sync_control_rejects_other_subject_or_malformed_payload():
    client = Client()
    store = GCSFitbitSyncState(client=client, bucket="test")
    current = store.read("self")
    with pytest.raises(ValueError, match="subject"):
        store.replace(current, SyncState(subject_key="other"))
    client.values["raw/fitbit/v1/_control/device-sync/self.json"] = (1, b"{}", {})
    with pytest.raises(ValueError, match="schema"):
        store.read("self")

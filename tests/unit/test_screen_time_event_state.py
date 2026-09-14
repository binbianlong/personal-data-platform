from dataclasses import replace
from datetime import timedelta

import pytest

from personal_data_platform.sources.screen_time.event_state import EventState, observation_for
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from tests.screen_time_helpers import _raw, _record


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 0, 1), (1, 2, 0)])
def test_compact_changes_reconstruct_out_of_order_snapshots_and_reparse(order):
    state = EventState()
    observations = []
    for index in range(3):
        raw = replace(
            _raw(), key=str(index), observed_at=_raw().observed_at + timedelta(seconds=index)
        )
        record = replace(_record(raw), event_key=str(index), bundle_id=f"app.{index}")
        batch = ScreenTimeBatch([record])
        observations.append((observation_for(raw, batch), [record]))
    for index in order:
        state.observe(*observations[index])
    assert [r["bundle_id"] for r in state.resolve().values()] == ["app.2"]
    state.observe(observations[1][0], [])
    assert [r["bundle_id"] for r in state.resolve().values()] == ["app.2"]
    state.observe(observations[2][0], [])
    assert state.resolve() == {}
    assert EventState(state.serialize()).resolve() == {}


def test_same_event_observations_do_not_duplicate_record_state():
    state = EventState()
    for index in range(10):
        raw = replace(
            _raw(), key=str(index), observed_at=_raw().observed_at + timedelta(seconds=index)
        )
        record = replace(_record(_raw()), object_key=raw.key, observed_at=raw.observed_at)
        state.observe(observation_for(raw, ScreenTimeBatch([record])), [record])
    assert state.db.execute("SELECT count(*) FROM changes").fetchone()[0] == 1
    assert state.db.execute("SELECT count(*) FROM record_values").fetchone()[0] == 1
    assert len(state.resolve()) == 1

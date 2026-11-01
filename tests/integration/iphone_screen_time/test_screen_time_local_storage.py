"""External payload and confirmed transaction acknowledgement boundaries."""

import pytest

from personal_data_platform.biome import runtime
from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.biome.raw import encode_segment_envelope
from personal_data_platform.loader.models import RawDecodeError
from personal_data_platform.sources.iphone_screen_time.adapter import IPhoneScreenTimeSource
from personal_data_platform.sources.iphone_screen_time.runtime import load_pending
from personal_data_platform.sources.iphone_screen_time.storage import LocalRepository
from tests.screen_time_helpers import NOW, ScreenTimeWarehouse, event, segb


def prepare(state, name="first", segment="b" * 64):
    return state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key=segment,
        raw_bytes=encode_segment_envelope(segb(event(name))[0]),
        observed_at=NOW,
        parser_version=IPhoneScreenTimeSource.parser_version,
    )


@pytest.mark.parametrize("stage", ["events", "commit_before", "commit_after", "ack"])
def test_crash_retry_keeps_bytes_and_oldest_pending_prevents_overtake(tmp_path, monkeypatch, stage):
    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    first = prepare(state)
    second = prepare(state, "second")
    warehouse = ScreenTimeWarehouse(tmp_path / "warehouse.duckdb", fail_at=stage)
    if stage == "ack":
        original = state.mark_loaded
        monkeypatch.setattr(
            state, "mark_loaded", lambda *a, **k: (_ for _ in ()).throw(OSError("ack lost"))
        )
    with pytest.raises(OSError):
        load_pending(state, warehouse)
    assert [p.identity for p in state.pending()] == [first.identity, second.identity]
    assert state.pending()[0].compressed_payload == first.compressed_payload
    assert warehouse.query_rows("SELECT bundle_id FROM base.iphone_screen_time_event") == (
        [("first",)] if stage in {"commit_after", "ack"} else []
    )
    warehouse.close()
    if stage == "ack":
        monkeypatch.setattr(state, "mark_loaded", original)
    warehouse = ScreenTimeWarehouse(tmp_path / "warehouse.duckdb")
    load_pending(state, warehouse)
    assert state.pending() == []
    assert warehouse.query_rows(
        "SELECT bundle_id, is_valid FROM base.iphone_screen_time_event ORDER BY bundle_id"
    ) == [("first", True), ("second", True)]
    assert list(state.payload_directory.glob("*.gz")) == []
    warehouse.close()


def test_pending_only_repository_drops_bytes_after_confirmed_ack(tmp_path):
    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    p = prepare(state)
    repo = LocalRepository(state=state, source=IPhoneScreenTimeSource())
    assert len(list(repo.list_raw("raw/iphone_screen_time/"))) == 1
    assert repo.get_raw(p.identity.object_key, generation=1) == p.compressed_payload
    with pytest.raises(ValueError, match="generation"):
        repo.get_raw(p.identity.object_key, generation=2)
    warehouse = ScreenTimeWarehouse()
    load_pending(state, warehouse)
    assert list(repo.list_raw("raw/iphone_screen_time/")) == []
    with pytest.raises(ValueError, match="unavailable"):
        repo.get_raw(p.identity.object_key, generation=1)
    warehouse.close()


def test_unsupported_parser_pending_fails_closed_before_remote_attempt(tmp_path):
    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    p = prepare(state)
    state.pin_parser(p.identity.object_key, "old-parser")
    warehouse = ScreenTimeWarehouse()

    class MustNotDecode(IPhoneScreenTimeSource):
        def decode(self, raw, payload):
            pytest.fail("incompatible pinned bytes must be rejected before decode")

    with pytest.raises(RuntimeError, match="compatible replay"):
        runtime.load_pending(state, warehouse, source=MustNotDecode())
    assert warehouse.query_rows("SELECT * FROM base.iphone_screen_time_event") == []
    assert state.pending()[0].compressed_payload == p.compressed_payload
    warehouse.close()


def test_lost_local_state_with_existing_base_requires_bootstrap(tmp_path):
    warehouse = ScreenTimeWarehouse()
    original = CollectorState(tmp_path / "original.db", source_id="iphone_screen_time")
    prepare(original)
    load_pending(original, warehouse)
    lost = CollectorState(tmp_path / "lost.db", source_id="iphone_screen_time")
    prepare(lost, "new")
    with pytest.raises(RuntimeError, match="bootstrap"):
        load_pending(lost, warehouse)
    assert warehouse.query_rows("SELECT bundle_id FROM base.iphone_screen_time_event") == [
        ("first",)
    ]
    assert len(lost.pending()) == 1
    warehouse.close()


def test_decode_failure_remains_unpinned_and_new_parser_replays_saved_bytes(tmp_path):
    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    pending = prepare(state)
    warehouse = ScreenTimeWarehouse()

    class UnsupportedSource(IPhoneScreenTimeSource):
        parser_version = "unsupported-parser"

        def decode(self, raw, payload):
            raise RawDecodeError("unsupported payload")

    with pytest.raises(RawDecodeError, match="unsupported payload"):
        runtime.load_pending(state, warehouse, source=UnsupportedSource())
    saved = state.pending()[0]
    assert saved.compressed_payload == pending.compressed_payload
    assert saved.parser_version is None
    assert warehouse.query_rows("SELECT * FROM base.iphone_screen_time_event") == []

    runtime.load_pending(state, warehouse, source=IPhoneScreenTimeSource())
    assert state.pending() == []
    assert warehouse.query_rows(
        "SELECT bundle_id, is_valid FROM base.iphone_screen_time_event"
    ) == [("first", True)]
    warehouse.close()

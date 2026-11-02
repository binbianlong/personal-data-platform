"""Compact local state preserves lifetime events and evidence-based corrections."""

from dataclasses import replace

from personal_data_platform.biome import runtime
from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.sources.iphone_screen_time.adapter import IPhoneScreenTimeSource
from tests.integration.iphone_screen_time.test_screen_time_local_storage import prepare
from tests.screen_time_helpers import ScreenTimeWarehouse


def test_lifetime_history_survives_thirty_replacements_with_bounded_local_state(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    warehouse = ScreenTimeWarehouse()
    source = IPhoneScreenTimeSource()
    for i in range(30):
        prepare(state, f"app.{i}")
        runtime.load_pending(state, warehouse, source=source)
    assert (
        warehouse.query_value("SELECT count(*) FROM base.iphone_screen_time_event WHERE is_valid")
        == 30
    )
    with state._connect() as db:
        assert db.execute("SELECT count(*) FROM file_state").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM event_reference").fetchone() == (1,)
    assert list(state.payload_directory.iterdir()) == []
    warehouse.close()


class CorrectedSource(IPhoneScreenTimeSource):
    parser_version = "corrected"

    def decode(self, raw, payload):
        batch = super().decode(raw, payload)
        return replace(batch, records=[], parser_version_override=self.parser_version)


def test_parser_correction_does_not_invalidate_other_segment_copy(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    warehouse = ScreenTimeWarehouse()
    source = IPhoneScreenTimeSource()
    prepare(state)
    prepare(state, segment="c" * 64)
    runtime.load_pending(state, warehouse, source=source)
    with state._connect() as db:
        assert db.execute("SELECT count(*) FROM event_reference").fetchone() == (2,)
    # Changing parser requeues identical source bytes; the duplicate physical copy protects it.
    prepare(state)  # Original parser is still a consecutive duplicate.
    from personal_data_platform.biome.raw import encode_segment_envelope
    from tests.screen_time_helpers import NOW, event, segb

    args = dict(
        device_key="a" * 64,
        stream=source.stream,
        raw_bytes=encode_segment_envelope(segb(event("first"))[0]),
        observed_at=NOW,
        parser_version="corrected",
    )
    state.prepare(segment_key="b" * 64, **args)
    runtime.load_pending(state, warehouse, source=CorrectedSource())
    assert warehouse.query_rows("SELECT is_valid FROM base.iphone_screen_time_event") == [(True,)]
    state.prepare(segment_key="c" * 64, **args)
    runtime.load_pending(state, warehouse, source=CorrectedSource())
    assert warehouse.query_rows("SELECT is_valid FROM base.iphone_screen_time_event") == [(False,)]
    assert warehouse.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 1
    warehouse.close()


def test_changed_content_with_new_parser_does_not_supply_negative_evidence(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    warehouse = ScreenTimeWarehouse()
    prepare(state)
    runtime.load_pending(state, warehouse, source=IPhoneScreenTimeSource())
    prepare(state, "changed")
    runtime.load_pending(state, warehouse, source=CorrectedSource())
    assert warehouse.query_rows(
        "SELECT bundle_id, is_valid FROM base.iphone_screen_time_event"
    ) == [("first", True)]
    warehouse.close()

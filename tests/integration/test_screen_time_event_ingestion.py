from __future__ import annotations

import gzip
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.loader.job import run_loader
from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.sources.screen_time.checkpoint import MemoryCheckpointStore
from personal_data_platform.sources.screen_time.event_state import EventState, observation_for
from personal_data_platform.sources.screen_time.ingestion import CheckpointError
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.legacy_screen_time import LegacyScreenTimeBatch
from tests.screen_time_helpers import Repository, event, segb, tombstone


def decode(repository, raw):
    return ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))


def warehouse_at(path):
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    return warehouse


def test_reobservations_store_one_event_and_only_state_changes(tmp_path):
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    repository = Repository()
    initial = repository.add("100", segb(event("app.once"))[0])
    initial_batch = decode(repository, initial)
    warehouse.load_object(initial, byte_size=1, batch=initial_batch)
    original = warehouse.query_rows("SELECT * FROM base.screen_time_event")
    state = warehouse.screen_time_ingestion.state
    changes = state.db.execute("SELECT count(*) FROM changes").fetchone()[0]
    for index in range(1, 20):
        raw = replace(
            initial,
            key=initial.key + str(index),
            observed_at=initial.observed_at + timedelta(seconds=index),
        )
        batch = replace(
            initial_batch,
            records=[
                replace(record, object_key=raw.key, observed_at=raw.observed_at)
                for record in initial_batch.records
            ],
        )
        assert warehouse.load_object(raw, byte_size=1, batch=batch) == 1
        assert warehouse.load_object(raw, byte_size=1, batch=batch) == 0
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event") == original
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_record_occurrence") == 0
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_segment_observation") == 0
    assert state.db.execute("SELECT count(*) FROM changes").fetchone()[0] == changes
    assert state.db.execute("SELECT count(*) FROM record_values").fetchone()[0] == 1
    assert (
        "original_payload"
        not in state.db.execute("SELECT document FROM record_values").fetchone()[0]
    )
    warehouse.close()
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    warehouse.open_screen_time_ingestion()
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event") == original
    warehouse.close()


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 0, 1), (1, 2, 0)])
def test_out_of_order_and_reparse_keep_later_snapshot(tmp_path, order):
    repository = Repository()
    raws = [repository.add("100", segb(event(f"app.{index}"))[0]) for index in range(3)]
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    for index in order:
        raw = raws[index]
        warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))
    assert warehouse.query_rows("SELECT bundle_id FROM base.screen_time_event WHERE is_active") == [
        ("app.2",)
    ]
    old = decode(repository, raws[0])
    warehouse.load_object(
        raws[0],
        byte_size=1,
        batch=replace(
            old, records=[replace(record, parser_version="next-parser") for record in old.records]
        ),
    )
    assert warehouse.query_rows("SELECT bundle_id FROM base.screen_time_event WHERE is_active") == [
        ("app.2",)
    ]
    warehouse.close()


class FailingStore(MemoryCheckpointStore):
    def __init__(self):
        super().__init__()
        self.writes = 0
        self.fail_at = None
        self.persist_before_failure = False

    def write(self, payload):
        self.writes += 1
        if self.writes == self.fail_at:
            if self.persist_before_failure:
                super().write(payload)
            raise OSError("checkpoint response lost")
        super().write(payload)


@pytest.mark.parametrize(
    "stage", ["prepare", "prepare_response", "warehouse", "ack", "ack_response"]
)
def test_interrupted_update_recovers_before_later_input(tmp_path, stage):
    database = tmp_path / "events.duckdb"
    warehouse = warehouse_at(database)
    store = FailingStore()
    warehouse.open_screen_time_ingestion(store)
    repository = Repository()
    raw = repository.add("100", segb(event("app.once"))[0])
    if stage.startswith("prepare"):
        store.fail_at = store.writes + 1
    elif stage.startswith("ack"):
        store.fail_at = store.writes + 2
    store.persist_before_failure = stage.endswith("response")
    if stage == "warehouse":
        warehouse._load_object = lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("database lost")
        )
    with pytest.raises(CheckpointError):
        warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))
    with pytest.raises(CheckpointError):
        warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))
    warehouse.close()
    warehouse = warehouse_at(database)
    warehouse.open_screen_time_ingestion(store)
    warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM ops.ingestion_metadata WHERE status='succeeded'"
        )
        == 1
    )
    assert EventState(store.read()).get("pending") is None
    assert warehouse.query_value("SELECT revision FROM ops.screen_time_checkpoint") == 1
    warehouse.close()


def test_missing_or_stale_checkpoint_cannot_silently_reset(tmp_path):
    database = tmp_path / "events.duckdb"
    warehouse = warehouse_at(database)
    store = MemoryCheckpointStore()
    warehouse.open_screen_time_ingestion(store)
    stale = store.read()
    repository = Repository()
    raw = repository.add("100", segb(event("app.once"))[0])
    warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))
    warehouse.close()
    for checkpoint in (None, stale, b"broken"):
        store.payload = checkpoint
        warehouse = warehouse_at(database)
        with pytest.raises(CheckpointError):
            warehouse.open_screen_time_ingestion(store)
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
        warehouse.close()


def test_cutover_keeps_old_rows_and_user_deletion_overrides_legacy(tmp_path, monkeypatch):
    database = tmp_path / "events.duckdb"
    warehouse = warehouse_at(database)
    repository = Repository()
    payload = event("app.legacy")
    segment, offset = segb(payload)
    raw = repository.add("100", segment)
    batch = decode(repository, raw)
    warehouse.load_object(
        raw,
        byte_size=1,
        batch=LegacyScreenTimeBatch(batch.records, batch.source_segment_name, batch.segment_kind),
    )
    legacy_rows = warehouse.query_rows("SELECT * FROM base.screen_time_record_occurrence")
    warehouse.open_screen_time_ingestion()
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 0
    # A different segment contains the same event. No extra event row is created on retry.
    copy = repository.add("200", segment)
    warehouse.load_object(copy, byte_size=1, batch=decode(repository, copy))
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    deletion = repository.add(
        "100", segb(tombstone("100", offset, len(payload)))[0], kind="tombstones", logical="f" * 64
    )
    warehouse.load_object(deletion, byte_size=1, batch=decode(repository, deletion))
    assert warehouse.query_rows("SELECT is_active FROM base.screen_time_event") == [(False,)]
    assert warehouse.query_rows("SELECT * FROM base.screen_time_record_occurrence") == legacy_rows
    warehouse.close()
    project = tmp_path / "dbt"
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "dbt",
        project,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages", ".user.yml"),
    )
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=project)
    warehouse = warehouse_at(database)
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_legacy_transition") == 1
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_transition") == 0
    assert warehouse.query_value("SELECT count(*) FROM marts.daily_screen_time") == 0
    warehouse.close()


def test_loader_stops_on_checkpoint_failure_without_marking_committed_object_failed(tmp_path):
    store = FailingStore()
    repository = Repository()
    repository.checkpoint_store = lambda warehouse: store
    repository.add("100", segb(event("app.first"))[0])
    repository.add("200", segb(event("app.second"))[0])
    warehouse = warehouse_at(tmp_path / "events.duckdb")
    store.fail_at = 3  # Initial checkpoint, prepared update, acknowledgement.
    with pytest.raises(CheckpointError):
        run_loader(repository, warehouse)
    assert warehouse.query_rows("SELECT status FROM ops.ingestion_metadata") == [("succeeded",)]
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    warehouse.close()


def test_delta_state_reparse_removes_event_and_preserves_successor():
    repository = Repository()
    raws = [repository.add("100", segb(event(f"app.{i}"))[0]) for i in range(3)]
    state = EventState()
    for raw in raws:
        batch = decode(repository, raw)
        state.observe(observation_for(raw, batch), list(batch.records))
    state.observe(observation_for(raws[1], ScreenTimeBatch([])), [])
    assert [value["bundle_id"] for value in state.resolve().values()] == ["app.2"]
    state.observe(observation_for(raws[2], ScreenTimeBatch([])), [])
    assert state.resolve() == {}


def test_scratch_repository_never_uses_production_checkpoint():
    from personal_data_platform.recovery.rebuild import _SnapshotRawRepository

    def unexpected(*args):
        raise AssertionError("production checkpoint accessed")

    repository = _SnapshotRawRepository(
        repository=SimpleNamespace(checkpoint_store=unexpected), observations=()
    )
    first, second = repository.checkpoint_store(None), repository.checkpoint_store(None)
    first.write(b"scratch")
    assert second.read() is None


def test_event_and_success_transaction_roll_back_together(tmp_path, monkeypatch):
    from personal_data_platform.sources.screen_time.ingestion import EventBatch

    database = tmp_path / "events.duckdb"
    warehouse = warehouse_at(database)
    store = MemoryCheckpointStore()
    warehouse.open_screen_time_ingestion(store)
    repository = Repository()
    raw = repository.add("100", segb(event("app.once"))[0])
    original_write = EventBatch.write

    def fail_after_write(self, *args, **kwargs):
        original_write(self, *args, **kwargs)
        raise RuntimeError("interrupted transaction")

    monkeypatch.setattr(EventBatch, "write", fail_after_write)
    with pytest.raises(CheckpointError):
        warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 0
    assert warehouse.query_value("SELECT revision FROM ops.screen_time_checkpoint") == 0
    warehouse.close()
    monkeypatch.setattr(EventBatch, "write", original_write)
    warehouse = warehouse_at(database)
    warehouse.open_screen_time_ingestion(store)
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    assert warehouse.query_value("SELECT status FROM ops.ingestion_metadata") == "succeeded"
    warehouse.close()

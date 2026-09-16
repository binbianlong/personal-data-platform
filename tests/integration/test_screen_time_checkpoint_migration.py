from __future__ import annotations

import gzip
import importlib.util
import shutil
import sys
import types
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from personal_data_platform.cli import main
from personal_data_platform.sources.screen_time import models
from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.storage.motherduck import (
    DEFAULT_MIGRATIONS,
    Warehouse,
    WarehouseConfig,
    connect,
)
from tests.legacy_screen_time import LegacyScreenTimeBatch
from tests.screen_time_helpers import NOW, Repository, event, segb, tombstone


@pytest.fixture
def predecessor(tmp_path):
    # Frozen, unmodified modules from the actual PR #12 runtime (c1ad5a9).
    package = "_pr12_screen_time"
    module = types.ModuleType(package)
    module.__path__ = [str(Path(__file__).parents[1] / "fixtures" / "pr12")]
    sys.modules[package] = module
    sys.modules[f"{package}.models"] = models
    for name in ("checkpoint", "event_state", "ingestion"):
        spec = importlib.util.spec_from_file_location(
            f"{package}.{name}", Path(module.__path__[0]) / f"{name}.py"
        )
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = loaded
        spec.loader.exec_module(loaded)
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in DEFAULT_MIGRATIONS.glob("00[1-5]*.sql"):
        shutil.copyfile(path, migrations / path.name)
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "warehouse.duckdb"))))
    warehouse.migrate(migrations)
    # PR #12's coordinator used the same transactional writer under a private name.
    warehouse._load_object = warehouse.load_object
    store = sys.modules[f"{package}.checkpoint"].MemoryCheckpointStore()
    ingestion_type = sys.modules[f"{package}.ingestion"].ScreenTimeIngestion
    repository = Repository()
    try:
        yield warehouse, repository, store, ingestion_type, tmp_path / "checkpoint.sqlite"
    finally:
        warehouse.close()
        for name in list(sys.modules):
            if name == package or name.startswith(package + "."):
                del sys.modules[name]


def decode(repository, raw):
    return ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))


def deleted_history(predecessor, reason, *, archive=False):
    warehouse, repository, store, ingestion_type, path = predecessor
    payload = event("app.deleted")
    content, offset = segb(payload)
    raw = repository.add("100", content)
    if archive:
        batch = decode(repository, raw)
        warehouse.load_object(
            raw,
            byte_size=1,
            batch=LegacyScreenTimeBatch(
                batch.records, batch.source_segment_name, batch.segment_kind
            ),
        )
    ingestion = ingestion_type(warehouse, store)
    ingestion.load(raw, byte_size=1, batch=decode(repository, raw))
    erased = repository.add("100", segb(b"\0" * len(payload), state=3, crc=123)[0])
    ingestion.load(erased, byte_size=1, batch=decode(repository, erased))
    deletion = repository.add(
        "100", segb(tombstone("100", offset, len(payload), reason=reason))[0], kind="tombstones"
    )
    ingestion.load(deletion, byte_size=1, batch=decode(repository, deletion))
    path.write_bytes(store.read())
    ingestion.close()
    return raw, deletion, content


def snapshot(warehouse):
    return {
        name: warehouse.query_rows(f"SELECT * FROM {name} ORDER BY ALL")
        for name in (
            "ops.schema_migration",
            "ops.screen_time_checkpoint",
            "ops.ingestion_metadata",
            "base.screen_time_event",
            "base.screen_time_record_occurrence",
            "base.screen_time_segment_observation",
        )
    }


@pytest.mark.parametrize("reason", [1, 2])
@pytest.mark.parametrize("archive", [False, True])
def test_pr12_deletion_survives_migration_without_raw_and_new_copy(predecessor, reason, archive):
    warehouse, repository, _, _, path = predecessor
    _, _, content = deleted_history(predecessor, reason, archive=archive)
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is (reason == 1)
    # All original Raw is past retention and absent; only checkpoint + warehouse remain.
    warehouse.connection.execute(
        "UPDATE ops.ingestion_metadata SET retention_expired_at = ?", [NOW + timedelta(days=91)]
    )
    repository.objects.clear()
    before = snapshot(warehouse)
    checkpoint_bytes = path.read_bytes()
    warehouse.migrate(screen_time_checkpoint=path)
    assert (
        warehouse.query_rows("SELECT * FROM base.screen_time_event")
        == before["base.screen_time_event"]
    )
    for table in (
        "ops.ingestion_metadata",
        "base.screen_time_record_occurrence",
        "base.screen_time_segment_observation",
    ):
        assert warehouse.query_rows(f"SELECT * FROM {table} ORDER BY ALL") == before[table]
    assert path.read_bytes() == checkpoint_bytes
    assert warehouse.query_value("SELECT count(*) FROM ops.screen_time_deletion_match") >= 1
    warehouse.migrate()
    copy = repository.add("200", content)
    warehouse.load_object(copy, byte_size=1, batch=decode(repository, copy))
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is (reason == 1)
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_transition") == (
        reason == 1
    )


def test_success_receipts_skip_old_raw_without_losing_deletion(predecessor):
    warehouse, repository, _, _, path = predecessor
    raw, deletion, content = deleted_history(predecessor, 2)
    warehouse.migrate(screen_time_checkpoint=path)
    for old in (raw, deletion):
        assert warehouse.load_object(old, byte_size=1, batch=decode(repository, old)) == 0
    copy = repository.add("200", content)
    warehouse.load_object(copy, byte_size=1, batch=decode(repository, copy))
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is False


@pytest.mark.parametrize("reobserve", ["none", "same_position", "different_position"])
def test_migrated_tombstone_can_be_corrected_by_reparsing_its_raw(predecessor, reobserve):
    warehouse, repository, _, _, path = predecessor
    _, deletion, _ = deleted_history(predecessor, 2)
    warehouse.migrate(screen_time_checkpoint=path)
    if reobserve != "none":
        payload = event("app.deleted")
        _, offset = segb(payload)
        deletion = repository.add(
            "100",
            segb(
                tombstone("100", offset, len(payload)),
                timestamp=20.0 if reobserve == "different_position" else 10.0,
            )[0],
            kind="tombstones",
        )
        warehouse.load_object(deletion, byte_size=1, batch=decode(repository, deletion))
    batch = decode(repository, deletion)
    corrected = replace(
        batch,
        records=[
            replace(record, deletion_reason=1, parser_version="corrected")
            for record in batch.records
        ],
    )
    warehouse.load_object(deletion, byte_size=1, batch=corrected)
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is (
        reobserve != "different_position"
    )


def test_unmatched_checkpoint_deletion_applies_to_target_arriving_after_migration(predecessor):
    warehouse, repository, store, ingestion_type, path = predecessor
    payload = event("app.late")
    content, offset = segb(payload)
    ingestion = ingestion_type(warehouse, store)
    deletion = repository.add(
        "100", segb(tombstone("100", offset, len(payload)))[0], kind="tombstones"
    )
    ingestion.load(deletion, byte_size=1, batch=decode(repository, deletion))
    path.write_bytes(store.read())
    ingestion.close()
    # No event exists yet: the marker must still prevent an import without checkpoint.
    with pytest.raises(RuntimeError, match="checkpoint required"):
        warehouse.migrate()
    warehouse.migrate(screen_time_checkpoint=path)
    raw = repository.add("100", content)
    warehouse.load_object(raw, byte_size=1, batch=decode(repository, raw))
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is False


def test_user_deletion_with_multiple_current_copies(predecessor):
    warehouse, repository, store, ingestion_type, path = predecessor
    payload = event("app.deleted")
    content, offset = segb(payload)
    ingestion = ingestion_type(warehouse, store)
    for name in ("100", "200"):
        raw = repository.add(name, content)
        ingestion.load(raw, byte_size=1, batch=decode(repository, raw))
    deletion = repository.add(
        "100", segb(tombstone("100", offset, len(payload)))[0], kind="tombstones"
    )
    ingestion.load(deletion, byte_size=1, batch=decode(repository, deletion))
    path.write_bytes(store.read())
    ingestion.close()
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is False
    warehouse.migrate(screen_time_checkpoint=path)
    copy = repository.add("300", content)
    warehouse.load_object(copy, byte_size=1, batch=decode(repository, copy))
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is False


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "identity",
        "revision",
        "pending",
        "format",
        "corrupt",
        "observations",
        "events",
        "record",
    ],
)
def test_invalid_checkpoint_rolls_back_and_valid_export_can_retry(predecessor, damage):
    warehouse, _, _, _, path = predecessor
    deleted_history(predecessor, 2)
    good = path.read_bytes()
    before = snapshot(warehouse)
    if damage == "corrupt":
        path.write_bytes(b"not sqlite")
    elif damage != "missing":
        import sqlite3

        with sqlite3.connect(path) as db:
            if damage in {"identity", "revision", "pending", "format"}:
                key, value = {
                    "identity": ("state_id", '"other"'),
                    "revision": ("revision", "0"),
                    "pending": ("pending", "{}"),
                    "format": ("format", "2"),
                }[damage]
                db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", [key, value])
            elif damage == "observations":
                db.execute("DELETE FROM observations")
            elif damage == "events":
                db.execute("DELETE FROM events")
            else:
                db.execute("UPDATE record_values SET document = '{}'")
    for _ in range(2):
        with pytest.raises(Exception):
            warehouse.migrate(screen_time_checkpoint=None if damage == "missing" else path)
        assert snapshot(warehouse) == before
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'screen_time_record'"
            )
            == 0
        )
    path.write_bytes(good)
    warehouse.migrate(screen_time_checkpoint=path)
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is False


def test_cli_migrates_local_checkpoint_and_rejects_already_migrated_target(
    predecessor, monkeypatch
):
    warehouse, _, _, _, path = predecessor
    deleted_history(predecessor, 2)
    monkeypatch.setenv("MOTHERDUCK_DATABASE", str(path.parent / "warehouse.duckdb"))
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    assert main(["screen-time", "migrate-checkpoint", "--checkpoint", str(path)]) == 0
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is False
    assert main(["screen-time", "migrate-checkpoint", "--checkpoint", str(path)]) == 1

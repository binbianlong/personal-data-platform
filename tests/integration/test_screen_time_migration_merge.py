from __future__ import annotations

import gzip
import hashlib
import shutil
from dataclasses import replace

import duckdb
import pytest

from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.sources.screen_time.event_state import EVENT_COLUMNS
from personal_data_platform.storage.motherduck import (
    DEFAULT_MIGRATIONS,
    Warehouse,
    WarehouseConfig,
    connect,
)
from tests.legacy_screen_time import LegacyScreenTimeBatch
from tests.screen_time_helpers import NOW, Repository, event, segb, tombstone

MIGRATION = "006_screen_time_ingestion.sql"
ORIGINAL_CHECKSUM = "abe9144642b57f906eaac49bd3ecef597ef4e43d4388fdc5bc1478c934bce7a1"


@pytest.fixture
def legacy(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in DEFAULT_MIGRATIONS.glob("00[1-5]*.sql"):
        shutil.copyfile(path, migrations / path.name)
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate(migrations)
    warehouse.connection.execute(
        "INSERT INTO ops.screen_time_checkpoint VALUES (true, 'existing-state', 1)"
    )
    try:
        yield warehouse, Repository(), migrations
    finally:
        warehouse.close()


def archive(warehouse, repository, name, content, **kwargs):
    raw = repository.add(name, content, **kwargs)
    batch = ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))
    warehouse.load_object(
        raw,
        byte_size=1,
        batch=LegacyScreenTimeBatch(batch.records, batch.source_segment_name, batch.segment_kind),
    )
    return raw, batch


def existing_event(warehouse, record, *, active=True, copies=0):
    row = {
        **{name: getattr(record, name, None) for name in EVENT_COLUMNS},
        "platform": "ios",
        "state": "start" if record.in_foreground else "end",
        "duplicate_occurrence_count": copies,
    }
    warehouse.connection.execute(
        "INSERT INTO base.screen_time_event VALUES (" + ", ".join("?" for _ in range(23)) + ")",
        [row[name] for name in EVENT_COLUMNS] + [active, NOW],
    )


def snapshot(warehouse):
    return {
        table: warehouse.query_rows(f"SELECT * FROM {table} ORDER BY ALL")
        for table in (
            "base.screen_time_event",
            "base.screen_time_record_occurrence",
            "base.screen_time_segment_observation",
            "ops.screen_time_checkpoint",
            "ops.ingestion_metadata",
            "ops.schema_migration",
        )
    }


def test_overlap_keeps_existing_row_and_adds_missing_events(legacy):
    warehouse, repository, _ = legacy
    _, batch = archive(warehouse, repository, "100", segb(event("app.existing"))[0])
    existing_event(warehouse, batch.records[0], copies=1)
    # Same event at another physical location: keep the existing provenance/loaded_at.
    archive(warehouse, repository, "200", segb(event("app.existing"))[0])
    archive(warehouse, repository, "300", segb(event("app.missing"))[0])
    before = snapshot(warehouse)
    warehouse.migrate()
    after = warehouse.query_rows("SELECT * FROM base.screen_time_event ORDER BY ALL")
    assert before["base.screen_time_event"][0] in after
    assert len(after) == 2
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_transition") == 2
    for table in ("base.screen_time_record_occurrence", "base.screen_time_segment_observation"):
        assert warehouse.query_rows(f"SELECT * FROM {table} ORDER BY ALL") == before[table]
    warehouse.migrate()
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event ORDER BY ALL") == after


@pytest.mark.parametrize("reason", [1, 2])
def test_overlap_preserves_supported_ttl_and_user_deletion(legacy, reason):
    warehouse, repository, _ = legacy
    payload = event("app.deleted")
    content, offset = segb(payload)
    _, batch = archive(warehouse, repository, "100", content)
    archive(warehouse, repository, "100", segb(b"\0" * len(payload), state=3, crc=123)[0])
    archive(
        warehouse,
        repository,
        "100",
        segb(tombstone("100", offset, len(payload), reason=reason))[0],
        kind="tombstones",
    )
    existing_event(warehouse, batch.records[0], active=reason == 1)
    before = warehouse.query_rows("SELECT * FROM base.screen_time_event")
    warehouse.migrate()
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event") == before
    copy = repository.add("200", content)
    batch = ScreenTimeSource().decode(copy, gzip.decompress(repository.objects[copy.key][1]))
    warehouse.load_object(copy, byte_size=1, batch=batch)
    assert warehouse.query_value("SELECT is_active FROM base.screen_time_event") is (reason == 1)


@pytest.mark.parametrize(
    "difference", ["deletion", "correction", "missing_record", "orphan_deleted", "copies"]
)
def test_unreconstructable_existing_state_rolls_back_and_can_retry(legacy, difference):
    warehouse, repository, _ = legacy
    _, batch = archive(warehouse, repository, "100", segb(event("app.existing"))[0])
    record = batch.records[0]
    if difference == "correction":
        record = replace(record, app_version="corrected", parser_version="next")
    if difference in ("missing_record", "orphan_deleted"):
        record = replace(record, event_key="corrected-key")
    existing_event(
        warehouse,
        record,
        active=difference not in ("deletion", "orphan_deleted"),
        copies=int(difference == "copies"),
    )
    before = snapshot(warehouse)
    for _ in range(2):
        with pytest.raises(
            duckdb.InvalidInputException, match="existing events cannot be reconstructed"
        ):
            warehouse.migrate()
        assert snapshot(warehouse) == before
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'ops' AND table_name = 'screen_time_record'"
            )
            == 0
        )


def test_conflict_retry_succeeds_after_restoring_corrected_history(legacy):
    warehouse, repository, _ = legacy
    raw, batch = archive(warehouse, repository, "100", segb(event("app.existing"))[0])
    corrected = replace(batch.records[0], app_version="corrected", parser_version="next")
    existing_event(warehouse, corrected)
    before = snapshot(warehouse)
    with pytest.raises(
        duckdb.InvalidInputException, match="existing events cannot be reconstructed"
    ):
        warehouse.migrate()
    assert snapshot(warehouse) == before
    warehouse.load_object(
        raw,
        byte_size=1,
        batch=LegacyScreenTimeBatch([corrected], batch.source_segment_name, batch.segment_kind),
    )
    warehouse.migrate()
    assert (
        warehouse.query_rows("SELECT * FROM base.screen_time_event")
        == before["base.screen_time_event"]
    )


def test_original_migration_checksum_remains_supported(legacy):
    warehouse, _, migrations = legacy
    fixed = (DEFAULT_MIGRATIONS / MIGRATION).read_text()
    original = fixed.split("-- Merge only reproducible existing events.")[0] + (
        "INSERT INTO base.screen_time_event\n"
        "SELECT *, current_timestamp FROM ops.screen_time_resolve(\n"
        "    (SELECT list(DISTINCT event_key) FROM ops.screen_time_record)\n);\n"
    )
    assert hashlib.sha256(original.encode()).hexdigest() == ORIGINAL_CHECKSUM
    (migrations / MIGRATION).write_text(original)
    warehouse.migrate(migrations)
    before = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")
    warehouse.migrate()
    assert (
        warehouse.query_rows(
            "SELECT * FROM ops.schema_migration WHERE migration_id <= ? ORDER BY migration_id",
            [MIGRATION],
        )
        == before
    )
    # Accept only the known original/fixed pair, not subsequent arbitrary SQL edits.
    (migrations / MIGRATION).write_text(fixed + "\n-- unrelated edit\n")
    with pytest.raises(RuntimeError, match="applied migration changed"):
        warehouse.migrate(migrations)


def test_unknown_applied_checksum_is_rejected(legacy):
    warehouse, _, _ = legacy
    warehouse.migrate()
    warehouse.connection.execute(
        "UPDATE ops.schema_migration SET checksum = 'unknown' WHERE migration_id = ?", [MIGRATION]
    )
    with pytest.raises(RuntimeError, match="applied migration changed"):
        warehouse.migrate()

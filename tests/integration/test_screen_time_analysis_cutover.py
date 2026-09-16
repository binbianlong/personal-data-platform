from __future__ import annotations

import gzip
import shutil
from dataclasses import replace

import duckdb
import pytest

from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.storage.motherduck import (
    DEFAULT_MIGRATIONS,
    Warehouse,
    WarehouseConfig,
    connect,
)
from tests.legacy_screen_time import LegacyScreenTimeBatch
from tests.screen_time_helpers import Repository, event, segb, tombstone

CUTOVER = "007_screen_time_analysis_entry.sql"
ARCHIVES = ("screen_time_segment_observation", "screen_time_record_occurrence")
RETIRED = (
    "screen_time_legacy_transition",
    "screen_time_tombstone_match",
    "screen_time_tombstone_status",
)


@pytest.fixture
def before_cutover(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in DEFAULT_MIGRATIONS.glob("00[1-5]*.sql"):
        shutil.copyfile(path, migrations / path.name)
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate(migrations)
    repository = Repository()
    for name, bundle, reason in [("100", "app.ttl", 1), ("200", "app.user", 2)]:
        payload = event(bundle)
        segment, offset = segb(payload)
        repository.add(name, segment, version=1)
        erased, _ = segb(b"\0" * len(payload), state=3, timestamp=20.0, crc=123)
        repository.add(name, erased)
        repository.add(
            name, segb(tombstone(name, offset, len(payload), reason=reason))[0], kind="tombstones"
        )
    repository.add("300", segb(event("app.user"))[0])
    # A superseded snapshot stays inactive; physical copies count once per event.
    repository.add("400", segb(event("app.old"))[0])
    repository.add("400", segb(event("app.current"))[0])
    repository.add("500", segb(event("app.current"))[0])
    source = ScreenTimeSource()
    for raw, content in repository.objects.values():
        batch = source.decode(raw, gzip.decompress(content))
        warehouse.load_object(
            raw,
            byte_size=1,
            batch=LegacyScreenTimeBatch(
                batch.records, batch.source_segment_name, batch.segment_kind
            ),
        )
    path = DEFAULT_MIGRATIONS / "006_screen_time_ingestion.sql"
    shutil.copyfile(path, migrations / path.name)
    warehouse.migrate(migrations)
    # Existing dependent views must remain readable until the next dbt run.
    for name in RETIRED[1:]:
        warehouse.connection.execute(f"CREATE VIEW base.{name} AS SELECT 1 AS value")
    warehouse.connection.execute(
        "CREATE VIEW base.screen_time_legacy_transition AS SELECT * EXCLUDE (is_active, loaded_at) "
        "FROM base.screen_time_event WHERE is_active"
    )
    warehouse.connection.execute(
        "CREATE VIEW base.screen_time_transition AS SELECT * FROM base.screen_time_legacy_transition"
    )
    warehouse.connection.execute(
        "CREATE VIEW base.cutover_consumer AS SELECT count(*) AS n FROM base.screen_time_transition"
    )
    try:
        yield warehouse, repository, source
    finally:
        warehouse.close()


def snapshot_archives(warehouse):
    return {
        name: warehouse.query_rows(f"SELECT * FROM base.{name} ORDER BY ALL") for name in ARCHIVES
    }


def test_cutover_preserves_history_and_dependents_and_is_repeatable(before_cutover):
    warehouse, _, _ = before_cutover
    archives = snapshot_archives(warehouse)
    expected = warehouse.query_rows("SELECT * FROM base.screen_time_transition ORDER BY event_key")
    warehouse.migrate()
    warehouse.migrate()
    assert snapshot_archives(warehouse) == archives
    assert (
        warehouse.query_rows("SELECT * FROM base.screen_time_transition ORDER BY event_key")
        == expected
    )
    assert warehouse.query_rows(
        "SELECT bundle_id, duplicate_occurrence_count FROM base.screen_time_transition ORDER BY bundle_id"
    ) == [("app.current", 1), ("app.ttl", 0)]
    assert warehouse.query_rows(
        "SELECT bundle_id FROM base.screen_time_event WHERE NOT is_active ORDER BY bundle_id"
    ) == [("app.old",), ("app.user",)]
    assert warehouse.query_value("SELECT n FROM base.cutover_consumer") == 2
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.views WHERE table_name IN (?, ?, ?)",
            list(RETIRED),
        )
        == 0
    )


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("DELETE FROM ops.screen_time_segment", "segment observations"),
        ("DELETE FROM ops.screen_time_record WHERE bundle_id = 'app.old'", "physical records"),
        ("DELETE FROM ops.screen_time_tombstone", "physical records"),
        ("DELETE FROM ops.screen_time_deletion_match", "deletion matches"),
        ("DELETE FROM base.screen_time_event WHERE bundle_id = 'app.ttl'", "analytical events"),
        (
            "UPDATE base.screen_time_event SET is_active = true WHERE bundle_id = 'app.user'",
            "analytical events",
        ),
        ("UPDATE base.screen_time_event SET duplicate_occurrence_count = 0", "analytical events"),
    ],
)
def test_incomplete_cutover_rolls_back_without_retiring_views(before_cutover, damage, message):
    warehouse, _, _ = before_cutover
    archives = snapshot_archives(warehouse)
    warehouse.connection.execute(damage)
    before = warehouse.query_rows("SELECT * FROM base.screen_time_transition ORDER BY event_key")
    for _ in range(2):
        with pytest.raises(duckdb.InvalidInputException, match=message):
            warehouse.migrate()
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM ops.schema_migration WHERE migration_id = ?", [CUTOVER]
            )
            == 0
        )
        assert snapshot_archives(warehouse) == archives
        assert (
            warehouse.query_rows("SELECT * FROM base.screen_time_transition ORDER BY event_key")
            == before
        )
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM information_schema.views WHERE table_name IN (?, ?, ?)",
                list(RETIRED),
            )
            == 3
        )


def test_cutover_accepts_live_reobservations_and_parser_key_corrections(before_cutover):
    warehouse, repository, source = before_cutover
    # Provenance is intentionally stale when analytical fields stay the same.
    raw = repository.add("500", segb(event("app.current"))[0])
    batch = source.decode(raw, gzip.decompress(repository.objects[raw.key][1]))
    warehouse.load_object(raw, byte_size=1, batch=batch)
    # The corrected-away key remains as inactive history without an ops record.
    raw = repository.add("600", segb(event("app.corrected"))[0])
    batch = source.decode(raw, gzip.decompress(repository.objects[raw.key][1]))
    warehouse.load_object(raw, byte_size=1, batch=batch)
    corrected = replace(
        batch,
        records=[
            replace(r, event_key="corrected-key", parser_version="next") for r in batch.records
        ],
    )
    warehouse.load_object(raw, byte_size=1, batch=corrected)
    warehouse.migrate()
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM base.screen_time_transition WHERE bundle_id = 'app.corrected'"
        )
        == 1
    )

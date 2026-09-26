from __future__ import annotations

import gzip
import shutil
from dataclasses import replace
from pathlib import Path

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.loader.job import run_loader
from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import Repository, event, mac_usage_event, segb, tombstone


def test_mac_tombstone_does_not_delete_iphone_event_in_another_stream() -> None:
    from personal_data_platform.sources.registry import get_source

    phone_repository = Repository()
    mac_repository = Repository()
    phone_repository.add("100", segb(event("shared.app"))[0])
    mac_payload = mac_usage_event("shared.app", 1_789_000_000.0, start=True)
    mac_segment, offset = segb(mac_payload)
    mac_repository.add("100", mac_segment, stream="app-usage")
    mac_repository.add(
        "200",
        segb(tombstone("100", offset, len(mac_payload)))[0],
        kind="tombstones",
        stream="app-usage",
    )
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate()
        assert run_loader(phone_repository, warehouse).failed == 0
        assert (
            run_loader(
                mac_repository, warehouse, source=get_source("screen_time", "app-usage")
            ).failed
            == 0
        )
        assert warehouse.query_rows(
            "SELECT platform, is_active FROM base.screen_time_event ORDER BY platform"
        ) == [("ios", True), ("macos", False)]
        assert warehouse.query_rows("SELECT resolution FROM ops.screen_time_tombstone") == [
            ("user_deletion_applied",)
        ]
    finally:
        warehouse.close()


def test_user_deletions_ttl_history_and_reused_positions(tmp_path, monkeypatch):
    repository = Repository()
    # Old v1 observations acquire a filename from the new v2 snapshot of the same logical segment.
    for name, bundle, reason in [("100", "app.user", 2), ("200", "app.ttl", 1)]:
        payload = event(bundle)
        segment, offset = segb(payload)
        repository.add(name, segment, version=1)
        erased, _ = segb(b"\0" * len(payload), state=3, timestamp=20.0, crc=123)
        repository.add(name, erased)
        repository.add(
            name, segb(tombstone(name, offset, len(payload), reason=reason))[0], kind="tombstones"
        )
    # A duplicate copy of a user-deleted logical event must not resurrect it.
    repository.add("300", segb(event("app.user"))[0])
    # Other devices are independent.
    repository.add("100", segb(event("app.user"))[0], device="b" * 64)
    # The same offset and length can now refer to a different event timestamp.
    old = event("app.slot", timestamp=10.0)
    old_segment, offset = segb(old)
    repository.add("400", old_segment)
    repository.add("400", segb(event("app.slot", timestamp=20.0), timestamp=20.0)[0])
    repository.add("400", segb(tombstone("400", offset, len(old)))[0], kind="tombstones")
    # Insufficient identities and unrecognized reasons never cause deletion.
    for name, field in [("500", "length"), ("600", "time"), ("700", "reason"), ("800", "name")]:
        payload = event("app." + name)
        segment, offset = segb(payload)
        repository.add(name, segment)
        deletion = tombstone(
            "999" if field == "name" else name,
            offset,
            len(payload) + 1 if field == "length" else len(payload),
            timestamp=99.0 if field == "time" else 10.0,
            reason=99 if field == "reason" else 2,
        )
        repository.add(name, segb(deletion)[0], kind="tombstones")

    database = tmp_path / "test.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    warehouse.migrate()
    result = run_loader(repository, warehouse)
    assert result.failed == 0
    assert result.succeeded == len(repository.objects)
    assert run_loader(repository, warehouse).skipped == len(repository.objects)
    warehouse.close()
    project = tmp_path / "dbt"
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "dbt",
        project,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages", ".user.yml"),
    )
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=project)
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_rows(
            "SELECT bundle_id FROM base.screen_time_transition ORDER BY bundle_id"
        ) == [
            (name,)
            for name in [
                "app.500",
                "app.600",
                "app.700",
                "app.800",
                "app.slot",
                "app.ttl",
                "app.user",
            ]
        ]
        assert (
            warehouse.query_value(
                "SELECT device_key FROM base.screen_time_transition WHERE bundle_id='app.user'"
            )
            == "b" * 64
        )
        assert (
            warehouse.query_value(
                "SELECT epoch(event_at) - 978307200 FROM base.screen_time_transition "
                "WHERE bundle_id='app.slot'"
            )
            == 20.0
        )
        assert dict(
            warehouse.query_rows(
                "SELECT resolution, count(*) FROM ops.screen_time_tombstone GROUP BY resolution"
            )
        ) == {
            "ttl_history_retained": 1,
            "unmatched": 3,
            "unsupported_reason": 1,
            "user_deletion_applied": 2,
        }

    finally:
        warehouse.close()


def test_parser_upgrade_reprocesses_available_raw_without_touching_other_history():
    repository = Repository()
    raw = repository.add("100", segb(event("app.upgrade"))[0])
    source = ScreenTimeSource()
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate()
        batch = source.decode(raw, gzip.decompress(repository.objects[raw.key][1]))
        batch = replace(
            batch, records=[replace(r, parser_version="app-in-focus-v1") for r in batch.records]
        )
        warehouse.load_object(raw, byte_size=1, batch=batch)
        assert run_loader(repository, warehouse).succeeded == 1
        assert run_loader(repository, warehouse).skipped == 1
        assert (
            warehouse.query_value("SELECT parser_version FROM ops.ingestion_metadata")
            == source.parser_version
        )
    finally:
        warehouse.close()

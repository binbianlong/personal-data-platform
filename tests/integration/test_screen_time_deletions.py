from __future__ import annotations

import gzip
import shutil
import struct
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.loader.job import run_loader
from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
from personal_data_platform.sources.screen_time.raw import (
    ScreenTimeRawIdentity,
    encode_segment_envelope,
    parse_raw_object_key,
    sha256_hex,
)
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def varint(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    return bytes(result) + bytes([n])


def text_field(tag, value):
    value = value.encode()
    return varint(tag * 8 + 2) + varint(len(value)) + value


def event(bundle, timestamp=10.0):
    return b"\x10\x01\x18\x01\x21" + struct.pack("<d", timestamp) + text_field(6, bundle)


def segb(payload, *, state=1, timestamp=10.0, crc=None):
    entry = struct.pack("<Ii", zlib.crc32(payload) if crc is None else crc, 0) + payload
    metadata_offset = 32 + len(entry) + (-len(entry) % 4)
    result = (
        struct.pack("<4sid16s", b"SEGB", 1, 0.0, b"\0" * 16)
        + entry
        + b"\0" * (-len(entry) % 4)
        + struct.pack("<2id", len(entry), state, timestamp)
    )
    return result, metadata_offset


def tombstone(name, offset, length, *, reason=2, timestamp=10.0):
    return (
        text_field(1, name)
        + b"\x10"
        + varint(offset)
        + b"\x18"
        + varint(length)
        + b"\x20"
        + varint(reason)
        + text_field(5, "synthetic")
        + b"\x31"
        + struct.pack("<d", timestamp)
    )


class Repository:
    def __init__(self):
        self.objects = {}

    def add(self, name, segment, *, kind="events", device="a" * 64, version=2, logical=None):
        value = encode_segment_envelope(segment, name=name, kind=kind) if version == 2 else segment
        identity = ScreenTimeRawIdentity(
            device_key=device,
            stream="app-in-focus",
            segment_key=logical or sha256_hex((kind + name).encode()),
            observed_at=NOW + timedelta(seconds=len(self.objects)),
            sha256=sha256_hex(value),
            schema_version=version,
        )
        raw = parse_raw_object_key(
            identity.object_key, storage_created_at=NOW, storage_generation=1
        )
        self.objects[raw.key] = raw, gzip.compress(value, mtime=0)
        return raw

    def list_raw(self, prefix):
        return [raw for raw, _ in self.objects.values() if raw.key.startswith(prefix)]

    def get_raw(self, key, *, generation):
        assert generation == 1
        return self.objects[key][1]


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
                "SELECT cf_absolute_time FROM base.screen_time_record_occurrence "
                "WHERE event_key=(SELECT event_key FROM base.screen_time_transition "
                "WHERE bundle_id='app.slot') LIMIT 1"
            )
            == 20.0
        )
        assert warehouse.query_rows(
            "SELECT status, count(*) FROM base.screen_time_tombstone_status GROUP BY status ORDER BY status"
        ) == [
            ("ttl_history_retained", 1),
            ("unmatched", 3),
            ("unsupported_reason", 1),
            ("user_deletion_applied", 2),
        ]
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


def test_migration_preserves_existing_occurrences_and_can_be_repeated(tmp_path):
    from personal_data_platform.sources.screen_time.writer import _record_row
    from personal_data_platform.storage.motherduck import DEFAULT_MIGRATIONS

    repository = Repository()
    raw = repository.add("100", segb(event("app.legacy"))[0])
    batch = ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))
    legacy = tmp_path / "migrations"
    legacy.mkdir()
    for filename in ("001_initial.sql", "002_raw_retention.sql", "003_source_ingestion.sql"):
        shutil.copyfile(DEFAULT_MIGRATIONS / filename, legacy / filename)
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate(legacy)
        row = _record_row(batch.records[0], NOW)[:26]
        warehouse.connection.execute(
            "INSERT INTO base.screen_time_record_occurrence VALUES (" + ",".join("?" * 26) + ")",
            row,
        )
        warehouse.connection.execute(
            "CREATE VIEW base.legacy_view AS SELECT event_key FROM base.screen_time_record_occurrence"
        )
        warehouse.migrate()
        warehouse.migrate()
        assert warehouse.query_rows("SELECT * FROM base.screen_time_record_occurrence")[0][
            :26
        ] == tuple(row)
        assert warehouse.query_value(
            "SELECT payload_length FROM base.screen_time_record_occurrence"
        ) == len(batch.records[0].original_payload)
        assert (
            warehouse.query_value("SELECT event_key FROM base.legacy_view")
            == batch.records[0].event_key
        )
    finally:
        warehouse.close()

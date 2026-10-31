#!/usr/bin/env python3
"""Split a stopped legacy collector's SQLite archive without changing its contents."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

SCHEMA = """
CREATE TABLE segment_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_key TEXT NOT NULL,
    stream TEXT NOT NULL,
    segment_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    compressed_payload BLOB,
    status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded')),
    uploaded_at TEXT,
    storage_created_at TEXT,
    storage_generation INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX segment_observation_latest
    ON segment_observation (device_key, stream, segment_key, id DESC);
CREATE INDEX segment_observation_pending ON segment_observation (status, id);
CREATE TABLE collector_stream_scan (
    stream TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL,
    device_keys TEXT NOT NULL,
    device_scanned_at TEXT NOT NULL
);
"""
SOURCES = {"app-in-focus": "iphone_screen_time", "app-usage": "mac_screen_time"}
TABLES = ("segment_observation", "collector_stream_scan", "sqlite_sequence")
RETIRED_TABLES = {
    "collector_control_configuration",
    "collector_control_object",
    "collector_control_publication",
    "collector_publication",
    "collector_scan",
}
KEY = re.compile(
    r"raw/screen_time/v2/([0-9a-f]{64})/(app-in-focus|app-usage)/"
    r"([0-9a-f]{64})/(\d{8}T\d{12}Z)/([0-9a-f]{64})\.segb\.gz"
)


def readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def schema_inventory(db: sqlite3.Connection) -> dict[tuple[str, str, str], str]:
    return {
        (kind, name, table): re.sub(r"\s+", "", sql).replace("IFNOTEXISTS", "")
        for kind, name, table, sql in db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        )
    }


def validate_schema(db: sqlite3.Connection, *, legacy: bool) -> list[str]:
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(SCHEMA)
        expected = schema_inventory(reference)
    actual = schema_inventory(db)
    extras = sorted({table for _, _, table in actual if table not in TABLES})
    if not legacy and extras or set(extras) - RETIRED_TABLES:
        raise ValueError(f"unexpected SQLite tables: {extras}")
    runtime = {key: value for key, value in actual.items() if key[2] in TABLES}
    if runtime != expected:
        raise ValueError("SQLite runtime schema differs from the supported collector schema")
    if any(kind not in {"table", "index"} for kind, _, _ in actual):
        raise ValueError("SQLite triggers and views are not supported")
    return extras


def inventory(db: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in TABLES}


def partitions(snapshot: dict[str, list[tuple]]) -> dict[str, dict[str, list[tuple]]]:
    result = {
        stream: {"segment_observation": [], "collector_stream_scan": [], "sqlite_sequence": []}
        for stream in SOURCES
    }
    keys: set[str] = set()
    for row in snapshot["segment_observation"]:
        _, device, stream, segment, observed, digest, key, payload, status, _, _, generation = row
        if stream not in SOURCES:
            raise ValueError(f"unknown observation stream: {stream}")
        match = KEY.fullmatch(key)
        if match is None or match.groups() != (device, stream, segment, observed, digest):
            raise ValueError(f"object key does not match observation identity: {key}")
        if status == "pending" and payload is None:
            raise ValueError(f"pending observation has no payload: {key}")
        if not isinstance(generation, int) or generation < 1:
            raise ValueError(f"invalid storage generation: {key}")
        mapped = key.replace("raw/screen_time/v2/", f"raw/{SOURCES[stream]}/v2/", 1)
        if mapped in keys:
            raise ValueError(f"object key collision: {mapped}")
        keys.add(mapped)
        result[stream]["segment_observation"].append(row[:6] + (mapped,) + row[7:])
    for row in snapshot["collector_stream_scan"]:
        if row[0] not in SOURCES:
            raise ValueError(f"unknown collector scan stream: {row[0]}")
        result[row[0]]["collector_stream_scan"].append(row)
    sequence = [row for row in snapshot["sqlite_sequence"] if row[0] == "segment_observation"]
    if (
        len(sequence) > 1
        or sequence
        and sequence[0][1] < max((row[0] for row in snapshot["segment_observation"]), default=0)
    ):
        raise ValueError("invalid segment_observation sequence highwater")
    for partition in result.values():
        partition["sqlite_sequence"] = sequence
    return result


def verify(path: Path, expected: dict[str, list[tuple]]) -> None:
    with readonly(path) as db:
        validate_schema(db, legacy=False)
        if inventory(db) != expected:
            raise ValueError(f"destination conflicts with expected partition: {path}")


def migrate(source: Path, iphone: Path, mac: Path, *, apply: bool) -> dict:
    source, iphone, mac = (path.expanduser().resolve() for path in (source, iphone, mac))
    if len({source, iphone, mac}) != 3:
        raise ValueError("source and destination paths must be distinct")
    with readonly(source) as db:
        db.execute("BEGIN")
        excluded = validate_schema(db, legacy=True)
        expected = partitions(inventory(db))
        destinations = {"app-in-focus": iphone, "app-usage": mac}
        for stream, destination in destinations.items():
            if destination.exists():
                verify(destination, expected[stream])
        temporary: dict[str, Path] = {}
        try:
            if apply:
                for stream, destination in destinations.items():
                    if destination.exists():
                        continue
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    descriptor, name = tempfile.mkstemp(
                        prefix=".collector-split-", suffix=".db", dir=destination.parent
                    )
                    os.close(descriptor)
                    temporary[stream] = Path(name)
                    with sqlite3.connect(name) as output:
                        output.executescript(SCHEMA)
                        for table, rows in expected[stream].items():
                            if table == "sqlite_sequence":
                                output.execute("DELETE FROM sqlite_sequence")
                            if rows:
                                marks = ",".join("?" for _ in rows[0])
                                output.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
                    verify(Path(name), expected[stream])
                    with open(name, "rb") as file:
                        os.fsync(file.fileno())
                # Both partitions have passed integrity and exact-value checks before publication.
                for stream, path in temporary.items():
                    destination = destinations[stream]
                    if destination.exists():
                        raise ValueError(f"destination appeared during migration: {destination}")
                    path.rename(destination)
                    directory = os.open(destination.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
        finally:
            for path in temporary.values():
                path.unlink(missing_ok=True)
    return {
        "applied": apply,
        "source_db": str(source),
        "excluded_legacy_tables": excluded,
        "destinations": {
            SOURCES[stream]: {
                "path": str(destination),
                "observations": len(expected[stream]["segment_observation"]),
                "scans": len(expected[stream]["collector_stream_scan"]),
            }
            for stream, destination in destinations.items()
        },
    }


def main() -> None:
    base = Path.home() / "Library/Application Support/personal-data-platform"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=base / "collector.db")
    parser.add_argument("--iphone-db", type=Path, default=base / "iphone_screen_time/collector.db")
    parser.add_argument("--mac-db", type=Path, default=base / "mac_screen_time/collector.db")
    parser.add_argument("--apply", action="store_true", help="publish both validated partitions")
    args = parser.parse_args()
    try:
        report = migrate(args.source_db, args.iphone_db, args.mac_db, apply=args.apply)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"migration failed: {error}\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

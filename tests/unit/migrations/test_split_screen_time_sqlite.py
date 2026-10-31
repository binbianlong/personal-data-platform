"""Offline SQLite split tests; no source runtime imports or production connections."""

import gzip
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/migrate_screen_time_sources.py"
SCHEMA = """
CREATE TABLE segment_observation (
 id INTEGER PRIMARY KEY AUTOINCREMENT, device_key TEXT NOT NULL, stream TEXT NOT NULL,
 segment_key TEXT NOT NULL, observed_at TEXT NOT NULL, sha256 TEXT NOT NULL,
 object_key TEXT NOT NULL UNIQUE, compressed_payload BLOB,
 status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded')), uploaded_at TEXT,
 storage_created_at TEXT, storage_generation INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX segment_observation_latest
 ON segment_observation (device_key, stream, segment_key, id DESC);
CREATE INDEX segment_observation_pending ON segment_observation (status, id);
CREATE TABLE collector_stream_scan (
 stream TEXT PRIMARY KEY, completed_at TEXT NOT NULL, device_keys TEXT NOT NULL,
 device_scanned_at TEXT NOT NULL
);
"""


@pytest.fixture
def state_paths(tmp_path):
    source = tmp_path / "collector.db"
    phone = tmp_path / "iphone_screen_time/collector.db"
    mac = tmp_path / "mac_screen_time/collector.db"
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        for ident, stream, status in (
            (7, "app-in-focus", "pending"),
            (18, "app-usage", "uploaded"),
            (40, "app-in-focus", "uploaded"),
        ):
            raw = b'PDPST\x02\x00\x00\x00\x19{"segment_kind":"events"}' + bytes([ident])
            digest = hashlib.sha256(raw).hexdigest()
            observed = f"20261030T0000{ident:02d}123456Z"
            key = f"raw/screen_time/v2/{'a' * 64}/{stream}/{'b' * 64}/{observed}/{digest}.segb.gz"
            db.execute(
                "INSERT INTO segment_observation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    "a" * 64,
                    stream,
                    "b" * 64,
                    observed,
                    digest,
                    key,
                    gzip.compress(raw, mtime=0),
                    status,
                    None if status == "pending" else observed,
                    "20261031T010203000004Z",
                    1_736_000_123_456_789 + ident,
                ),
            )
        db.execute("UPDATE sqlite_sequence SET seq = 999 WHERE name='segment_observation'")
        db.executemany(
            "INSERT INTO collector_stream_scan VALUES (?,?,?,?)",
            [
                ("app-in-focus", "20261031T010203000004Z", '["a"]', '{"a":"old"}'),
                ("app-usage", "20261031T010203000005Z", "[]", "{}"),
            ],
        )
    return source, phone, mac


def run(paths, *options):
    source, phone, mac = paths
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source-db",
            str(source),
            "--iphone-db",
            str(phone),
            "--mac-db",
            str(mac),
            *options,
        ],
        capture_output=True,
        text=True,
    )


def rows(path, table):
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def test_default_dry_run_validates_without_creating_destinations(state_paths):
    before = state_paths[0].read_bytes()
    result = run(state_paths)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["applied"] is False
    assert not state_paths[1].parent.exists()
    assert not state_paths[2].parent.exists()
    assert state_paths[0].read_bytes() == before


def test_apply_preserves_every_value_and_sequence_highwater(state_paths):
    before = state_paths[0].read_bytes()
    original = rows(state_paths[0], "segment_observation")
    result = run(state_paths, "--apply")
    assert result.returncode == 0, result.stderr
    for destination, stream, source_id in (
        (state_paths[1], "app-in-focus", "iphone_screen_time"),
        (state_paths[2], "app-usage", "mac_screen_time"),
    ):
        expected = [
            row[:6] + (row[6].replace("raw/screen_time/v2/", f"raw/{source_id}/v2/", 1),) + row[7:]
            for row in original
            if row[2] == stream
        ]
        assert rows(destination, "segment_observation") == expected
        assert rows(destination, "collector_stream_scan") == [
            row for row in rows(state_paths[0], "collector_stream_scan") if row[0] == stream
        ]
        assert rows(destination, "sqlite_sequence") == [("segment_observation", 999)]
        assert destination.stat().st_mode & 0o777 == 0o600
    assert state_paths[0].read_bytes() == before


def test_exact_rerun_and_partial_publish_are_recoverable(state_paths):
    assert run(state_paths, "--apply").returncode == 0
    phone_before = state_paths[1].read_bytes()
    state_paths[2].unlink()
    result = run(state_paths, "--apply")
    assert result.returncode == 0, result.stderr
    assert state_paths[1].read_bytes() == phone_before
    assert state_paths[2].exists()
    assert run(state_paths, "--apply").returncode == 0


def test_conflict_aborts_before_publishing_any_new_destination(state_paths):
    state_paths[2].parent.mkdir()
    state_paths[2].write_bytes(b"not a sqlite database")
    result = run(state_paths, "--apply")
    assert result.returncode != 0
    assert not state_paths[1].exists()
    assert state_paths[2].read_bytes() == b"not a sqlite database"


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE segment_observation SET stream='unknown' WHERE id=7",
        "UPDATE collector_stream_scan SET stream='unknown' WHERE stream='app-usage'",
        "UPDATE segment_observation SET object_key=replace(object_key,'raw/screen_time/','raw/other/') WHERE id=7",
        "UPDATE segment_observation SET object_key=replace(object_key,'/app-in-focus/','/app-usage/') WHERE id=7",
        "UPDATE segment_observation SET sha256=printf('%064d',0) WHERE id=7",
        "ALTER TABLE segment_observation ADD COLUMN unrecognized TEXT",
        "CREATE TRIGGER unexpected AFTER DELETE ON segment_observation BEGIN SELECT 1; END",
        "UPDATE segment_observation SET compressed_payload=NULL WHERE id=7",
    ],
)
def test_rejects_unrecognized_or_inconsistent_inventory(state_paths, mutation):
    with sqlite3.connect(state_paths[0]) as db:
        db.execute(mutation)
    result = run(state_paths, "--apply")
    assert result.returncode != 0
    assert not state_paths[1].exists()
    assert not state_paths[2].exists()


def test_changed_destination_row_is_conflict_even_on_dry_run(state_paths):
    assert run(state_paths, "--apply").returncode == 0
    with sqlite3.connect(state_paths[1]) as db:
        db.execute("UPDATE segment_observation SET storage_generation=42 WHERE id=7")
    assert run(state_paths).returncode != 0


def test_source_and_destinations_must_be_distinct(state_paths):
    result = run((state_paths[0], state_paths[0], state_paths[2]), "--apply")
    assert result.returncode != 0


def test_scan_only_partition_and_compacted_empty_archive_keep_highwater(state_paths):
    with sqlite3.connect(state_paths[0]) as db:
        db.execute("DELETE FROM segment_observation")
    result = run(state_paths, "--apply")
    assert result.returncode == 0, result.stderr
    for destination in state_paths[1:]:
        assert rows(destination, "segment_observation") == []
        assert rows(destination, "sqlite_sequence") == [("segment_observation", 999)]
        assert len(rows(destination, "collector_stream_scan")) == 1


def test_known_retired_control_tables_are_reported_and_not_copied(state_paths):
    with sqlite3.connect(state_paths[0]) as db:
        db.execute("CREATE TABLE collector_scan (historic TEXT)")
        db.execute("INSERT INTO collector_scan VALUES ('untouched')")
    before = state_paths[0].read_bytes()
    result = run(state_paths, "--apply")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["excluded_legacy_tables"] == ["collector_scan"]
    assert state_paths[0].read_bytes() == before
    with sqlite3.connect(state_paths[1]) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='collector_scan'").fetchall()
            == []
        )


def test_publication_interruption_can_resume_without_overwriting_complete_partition(
    state_paths,
    monkeypatch,
):
    import importlib.util

    spec = importlib.util.spec_from_file_location("split_sqlite", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_rename = Path.rename

    def fail_second_publish(path, destination):
        if destination == state_paths[2]:
            raise OSError("simulated interrupted publication")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_second_publish)
    with pytest.raises(OSError, match="interrupted publication"):
        module.migrate(*state_paths, apply=True)
    assert state_paths[1].exists()
    assert not state_paths[2].exists()
    assert list(state_paths[2].parent.iterdir()) == []
    monkeypatch.setattr(Path, "rename", original_rename)
    assert run(state_paths, "--apply").returncode == 0


def test_both_temporary_partitions_must_validate_before_either_is_published(
    state_paths,
    monkeypatch,
):
    import importlib.util

    spec = importlib.util.spec_from_file_location("split_sqlite", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_verify = module.verify

    def reject_second_temporary(path, expected):
        if path.parent == state_paths[2].parent:
            raise ValueError("simulated destination validation failure")
        original_verify(path, expected)

    monkeypatch.setattr(module, "verify", reject_second_temporary)
    with pytest.raises(ValueError, match="destination validation failure"):
        module.migrate(*state_paths, apply=True)
    for destination in state_paths[1:]:
        assert not destination.exists()
        assert list(destination.parent.iterdir()) == []

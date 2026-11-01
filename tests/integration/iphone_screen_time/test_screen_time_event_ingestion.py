"""Source writers preserve event history without remote operational tables."""

import gzip
from dataclasses import replace
from datetime import timedelta

import duckdb
import pytest

from personal_data_platform.sources.iphone_screen_time.adapter import IPhoneScreenTimeSource
from personal_data_platform.sources.iphone_screen_time.motherduck import write_state
from tests.screen_time_helpers import Repository, event, segb


@pytest.fixture
def db():
    connection = duckdb.connect()
    connection.execute("CREATE SCHEMA base")
    connection.execute(
        "CREATE TABLE base.iphone_screen_time_event (event_key VARCHAR PRIMARY KEY, device_key VARCHAR, bundle_id VARCHAR, event_at TIMESTAMPTZ, state VARCHAR, is_valid BOOLEAN)"
    )
    yield connection
    connection.close()


def decoded(repository, raw):
    return IPhoneScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))


def test_replay_changes_only_actual_rows_and_keeps_fractional_time(db):
    repository = Repository()
    raw = repository.add("100", segb(event("app.once", timestamp=10.125))[0])
    batch = decoded(repository, raw)
    assert write_state(db, raw, batch) == 1
    assert write_state(db, raw, batch) == 0
    before = db.execute("SELECT * FROM base.iphone_screen_time_event").fetchall()
    assert before[0][3].microsecond == 125000
    changed = replace(batch, records=[replace(batch.records[0], bundle_id="changed")])
    assert write_state(db, raw, changed) == 1
    assert db.execute("SELECT bundle_id FROM base.iphone_screen_time_event").fetchall() == [
        ("changed",)
    ]


def test_many_physical_duplicates_resolve_one_event_and_latest_slot(db):
    repository = Repository()
    raw = repository.add("100", segb(event("app.copy"))[0])
    batch = decoded(repository, raw)
    record = batch.records[0]
    copies = [
        replace(record, record_offset=32 + i * 100, record_metadata_offset=100000 + i * 16)
        for i in range(2001)
    ]
    assert write_state(db, raw, replace(batch, records=copies)) == 1
    # A later deleted physical slot removes only that slot; a duplicate remains valid.
    copies.append(replace(copies[0], record_metadata_offset=200000, record_state="DELETED"))
    assert write_state(db, raw, replace(batch, records=copies)) == 0
    assert db.execute(
        "SELECT count(*) FROM base.iphone_screen_time_event WHERE is_valid"
    ).fetchone() == (1,)


def test_ordinary_content_changes_omission_and_tombstones_keep_lifetime_history(db):
    repository = Repository()
    for i in range(30):
        raw = repository.add("100", segb(event(f"app.{i}"))[0])
        write_state(db, raw, decoded(repository, raw))
    write_state(db, raw, replace(decoded(repository, raw), records=[]))
    write_state(db, raw, replace(decoded(repository, raw), segment_kind="tombstones"))
    assert db.execute(
        "SELECT count(*) FROM base.iphone_screen_time_event WHERE is_valid"
    ).fetchone() == (30,)
    assert (
        db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='ops'"
        ).fetchall()
        == []
    )


def test_explicit_correction_invalidates_known_key_but_does_not_delete(db):
    repository = Repository()
    raw = repository.add("100", segb(event("old"))[0])
    batch = decoded(repository, raw)
    old = batch.records[0]
    write_state(db, raw, batch)
    corrected = replace(batch, records=[replace(old, event_key="corrected", bundle_id="new")])
    assert write_state(db, raw, corrected, invalidated_event_keys=[old.event_key]) == 2
    assert db.execute(
        "SELECT bundle_id, is_valid FROM base.iphone_screen_time_event ORDER BY bundle_id"
    ).fetchall() == [("new", True), ("old", False)]
    assert write_state(db, raw, corrected, invalidated_event_keys=[old.event_key]) == 0


def test_unsupported_records_never_create_base_rows(db):
    repository = Repository()
    raw = repository.add("100", segb(event("app"))[0])
    batch = decoded(repository, raw)
    for changes in (
        {"record_state": "DELETED"},
        {"crc_passed": False},
        {"record_kind": "unknown"},
        {"event_key": None},
    ):
        assert (
            write_state(db, raw, replace(batch, records=[replace(batch.records[0], **changes)]))
            == 0
        )
    assert db.execute("SELECT count(*) FROM base.iphone_screen_time_event").fetchone() == (0,)


def test_writer_leaves_transaction_ownership_to_caller(db):
    repository = Repository()
    raw = repository.add("100", segb(event("app"))[0])
    db.execute("BEGIN")
    write_state(db, raw, decoded(repository, raw))
    db.execute("ROLLBACK")
    assert db.execute("SELECT count(*) FROM base.iphone_screen_time_event").fetchone() == (0,)


def test_large_batch_uses_one_columnar_statement(db):
    repository = Repository()
    raw = repository.add("100", segb(event("app"))[0])
    batch = decoded(repository, raw)
    prototype = batch.records[0]
    records = [
        replace(
            prototype,
            event_key=f"{i:064x}",
            record_offset=32 + i * 100,
            record_metadata_offset=500000 + i * 16,
            event_at=prototype.event_at + timedelta(seconds=i),
        )
        for i in range(2001)
    ]

    class Remote:
        inserts = 0

        def execute(self, sql, *args):
            if "INSERT INTO base." in sql:
                self.inserts += 1
            return db.execute(sql, *args)

        def executemany(self, *args):
            pytest.fail("remote writes must be columnar")

    remote = Remote()
    assert write_state(remote, raw, replace(batch, records=records)) == 2001
    assert remote.inserts == 1

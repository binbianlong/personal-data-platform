from dataclasses import replace
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window
from personal_data_platform.sources.fitbit.writer import FitbitBatch
from personal_data_platform.storage.motherduck import Warehouse

AT = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def warehouse():
    value = Warehouse(duckdb.connect())
    value.migrate()
    yield value
    value.close()


def point(identity, minute, value=10):
    start = AT + timedelta(minutes=minute)
    return Record(
        "steps",
        identity,
        start,
        start,
        start + timedelta(minutes=1),
        value,
        offset_seconds=0,
        end_offset_seconds=0,
        source_date=start.date(),
    )


def snapshot(records, *, start=0, end=1440, version=1):
    return Snapshot(
        "self",
        Window("steps", AT + timedelta(minutes=start), AT + timedelta(minutes=end)),
        AT + timedelta(days=version),
        tuple(records),
    )


def apply(warehouse, data):
    warehouse.connection.execute("BEGIN")
    try:
        result = FitbitBatch(data).write_snapshot(
            warehouse.connection,
            source_key=str(data.fetched_at),
            loaded_at=data.fetched_at,
        )
        warehouse.connection.execute("COMMIT")
        return result
    except Exception:
        warehouse.connection.execute("ROLLBACK")
        raise


def test_full_partial_full_submits_only_changed_steps(warehouse):
    original = snapshot([point("a", 0), point("b", 1)])
    assert apply(warehouse, original).submitted_rows == 2
    changed = point("b", 1, 20)
    assert apply(warehouse, snapshot([changed], start=1, end=2, version=2)).submitted_rows == 1
    assert apply(warehouse, snapshot([point("a", 0), changed], version=3)).submitted_rows == 0
    assert warehouse.query_rows(
        "SELECT record_id,value,fetched_at FROM base.fitbit_activity_interval ORDER BY record_id"
    ) == [("a", 10.0, original.fetched_at), ("b", 20.0, AT + timedelta(days=2))]
    assert apply(warehouse, snapshot([point("a", 0, 99), changed], version=2)).submitted_rows == 0


def test_changed_full_scope_preserves_equal_row_metadata(warehouse):
    original = snapshot([point("a", 0), point("b", 1)])
    apply(warehouse, original)
    result = apply(warehouse, snapshot([point("a", 0), point("b", 1, 20)], version=2))
    assert result.submitted_rows == 1
    assert warehouse.query_rows(
        "SELECT fetched_at,loaded_at FROM base.fitbit_activity_interval WHERE record_id='a'"
    ) == [(original.fetched_at, original.fetched_at)]


def test_empty_partial_steps_only_deletes_inside_range(warehouse):
    apply(warehouse, snapshot([point("a", 0), point("b", 1), point("c", 2)]))
    result = apply(warehouse, snapshot([], start=1, end=2, version=2))
    assert result.submitted_rows == 0
    assert warehouse.query_rows(
        "SELECT record_id FROM base.fitbit_activity_interval ORDER BY record_id"
    ) == [("a",), ("c",)]
    assert warehouse.query_rows("SELECT record_id FROM ops.fitbit_deleted_record") == [("b",)]


def test_step_identity_move_invalidates_old_range_hash(warehouse):
    original = snapshot([point("a", 0)])
    apply(warehouse, original)
    moved = point("a", 1440)
    assert apply(warehouse, snapshot([moved], start=1440, end=1441, version=3)).submitted_rows == 1
    assert (
        apply(warehouse, replace(original, fetched_at=AT + timedelta(days=2))).submitted_rows == 0
    )
    assert (
        warehouse.query_value("SELECT start_at FROM base.fitbit_activity_interval") == moved.start
    )
    assert (
        apply(warehouse, replace(original, fetched_at=AT + timedelta(days=4))).submitted_rows == 1
    )
    assert warehouse.query_value("SELECT start_at FROM base.fitbit_activity_interval") == AT


def test_changed_step_offsets_are_submitted(warehouse):
    first = point("a", 0)
    apply(warehouse, snapshot([first]))
    changed = replace(first, offset_seconds=3600, end_offset_seconds=3600)
    assert apply(warehouse, snapshot([changed], version=2)).submitted_rows == 1
    assert warehouse.query_value("SELECT offset_seconds FROM base.fitbit_activity_interval") == 3600


def test_steps_rollback_restores_rows_deletions_and_coverage(warehouse):
    apply(warehouse, snapshot([point("a", 0), point("b", 1)]))
    before = {
        table: warehouse.query_rows("SELECT * FROM " + table)
        for table in [
            "base.fitbit_activity_interval",
            "ops.fitbit_coverage",
            "ops.fitbit_deleted_record",
        ]
    }
    data = snapshot([point("a", 0, 20)], version=2)
    warehouse.connection.execute("BEGIN")
    FitbitBatch(data).write_snapshot(
        warehouse.connection,
        source_key="rollback",
        loaded_at=data.fetched_at,
    )
    warehouse.connection.execute("ROLLBACK")
    for table, rows in before.items():
        assert warehouse.query_rows("SELECT * FROM " + table) == rows


def civil_snapshot(records, *, day=0, version=1):
    return Snapshot(
        "self",
        Window("steps", AT + timedelta(days=day), AT + timedelta(days=day + 1), time_basis="civil"),
        AT + timedelta(days=version),
        tuple(records),
    )


def local_point(identity, minute, value=10, offset=32400):
    start = AT + timedelta(minutes=minute)
    return Record(
        "steps",
        identity,
        start,
        start,
        start + timedelta(minutes=1),
        value,
        offset_seconds=offset,
        end_offset_seconds=offset,
        source_date=(start + timedelta(seconds=offset)).date(),
    )


def test_civil_steps_use_source_date_without_changing_utc_storage(warehouse):
    row = local_point("a", -540)
    result = apply(warehouse, civil_snapshot([row]))
    assert result.submitted_rows == 1
    assert warehouse.query_rows(
        "SELECT start_at,source_date FROM base.fitbit_activity_interval"
    ) == [(row.start, AT.date())]
    assert (
        warehouse.query_value("SELECT aggregation_version FROM ops.fitbit_coverage")
        == "records-civil-v1"
    )


def test_civil_empty_prevents_stale_unknown_utc_identity(warehouse):
    apply(warehouse, civil_snapshot([], version=3))
    row = local_point("unknown", -539)
    older = snapshot([row], start=-540, end=-538, version=2)
    assert apply(warehouse, older).submitted_rows == 0
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_activity_interval") == 0
    assert apply(warehouse, replace(older, fetched_at=AT + timedelta(days=4))).submitted_rows == 1


def test_utc_empty_prevents_stale_unknown_civil_identity(warehouse):
    apply(warehouse, snapshot([], start=-540, end=-538, version=3))
    row = local_point("unknown", -539)
    assert apply(warehouse, civil_snapshot([row], version=2)).submitted_rows == 0
    assert apply(warehouse, civil_snapshot([row], version=4)).submitted_rows == 1


def test_civil_confirmation_protects_equal_rows_and_unknown_ids_from_old_utc_result(warehouse):
    row = local_point("a", -540)
    apply(warehouse, snapshot([row], start=-540, end=-538))
    assert apply(warehouse, civil_snapshot([row], version=3)).submitted_rows == 0
    older = snapshot(
        [replace(row, value=99), local_point("unknown", -539)], start=-540, end=-538, version=2
    )
    assert apply(warehouse, older).submitted_rows == 0
    assert warehouse.query_rows("SELECT record_id,value FROM base.fitbit_activity_interval") == [
        ("a", 10.0)
    ]


def test_civil_empty_only_deletes_its_provider_date(warehouse):
    left = local_point("left", -541)
    right = local_point("right", -540)
    apply(warehouse, snapshot([left, right], start=-542, end=-538))
    assert apply(warehouse, civil_snapshot([], version=2)).submitted_rows == 0
    assert warehouse.query_rows("SELECT record_id FROM base.fitbit_activity_interval") == [
        ("left",)
    ]


def test_civil_empty_rejects_ambiguous_stored_dates_without_deleting(warehouse):
    row = replace(point("a", 0), source_date=None, offset_seconds=None)
    apply(warehouse, snapshot([row]))
    with pytest.raises(ValueError, match="civil|date"):
        apply(warehouse, civil_snapshot([], version=2))
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_activity_interval") == 1


def test_civil_change_invalidates_legacy_utc_hash(warehouse):
    old = local_point("a", -540)
    apply(warehouse, snapshot([old], start=-540, end=-538))
    assert apply(warehouse, civil_snapshot([replace(old, value=20)], version=2)).submitted_rows == 1
    assert (
        warehouse.query_value(
            "SELECT content_sha256 FROM ops.fitbit_coverage WHERE aggregation_version='records-v1'"
        )
        == ""
    )

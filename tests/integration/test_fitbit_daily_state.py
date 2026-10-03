from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit.models import Window
from personal_data_platform.sources.fitbit.state import CheckedWindow, DailyState, DailyStateStore
from personal_data_platform.storage.motherduck import Warehouse
from tests.sql_helpers import TracedConnection

NOW = datetime(2026, 10, 3, tzinfo=UTC)


@pytest.fixture
def warehouse():
    warehouse = Warehouse(duckdb.connect())
    warehouse.migrate()
    yield warehouse
    warehouse.close()


def coverage(warehouse, window, digest, *, subject="self", fetched_at=NOW, origin="api"):
    warehouse.connection.execute(
        "INSERT INTO ops.fitbit_coverage VALUES (?,?,?,?,?,?,?,?,?)",
        [
            subject,
            window.data_type,
            window.start,
            window.end,
            fetched_at,
            origin,
            "raw",
            "",
            digest,
        ],
    )


def test_skipped_batch_updates_only_matching_scopes_with_the_newest_check(warehouse):
    first = Window("steps", NOW, NOW + timedelta(days=1))
    second = Window("steps", NOW + timedelta(days=1), NOW + timedelta(days=2))
    third = Window("heart-rate", NOW, NOW + timedelta(days=1))
    coverage(warehouse, first, "a")
    coverage(warehouse, first, "a", subject="other")
    coverage(warehouse, second, "new-hash")
    coverage(warehouse, third, "c", fetched_at=NOW + timedelta(hours=2))
    connection = TracedConnection(warehouse.connection)
    warehouse.connection = connection

    DailyStateStore(warehouse, "self").finish(
        DailyState("self", last_daily_run=date(2026, 10, 3)),
        (
            CheckedWindow(first, NOW + timedelta(hours=1), "a"),
            CheckedWindow(first, NOW + timedelta(minutes=30), "a"),
            CheckedWindow(second, NOW + timedelta(hours=1), "old-hash"),
            CheckedWindow(third, NOW + timedelta(hours=1), "c"),
            CheckedWindow(Window("steps", first.start, first.end + timedelta(days=1)), NOW, "a"),
        ),
    )

    assert warehouse.query_rows(
        "SELECT subject_key, data_type, range_start, fetched_at FROM ops.fitbit_coverage "
        "ORDER BY subject_key, data_type, range_start"
    ) == [
        ("other", "steps", NOW, NOW),
        ("self", "heart-rate", NOW, NOW + timedelta(hours=2)),
        ("self", "steps", NOW, NOW + timedelta(hours=1)),
        ("self", "steps", NOW + timedelta(days=1), NOW),
    ]
    assert DailyStateStore(warehouse, "self").read().last_daily_run == date(2026, 10, 3)
    assert sum("UPDATE ops.fitbit_coverage" in sql for sql in connection.statements) == 1


def test_skipped_batch_and_checkpoint_roll_back_if_retiring_an_intent_fails(warehouse):
    first = Window("steps", NOW, NOW + timedelta(days=1))
    second = Window("heart-rate", NOW, NOW + timedelta(days=1))
    coverage(warehouse, first, "a")
    coverage(warehouse, second, "b")
    warehouse.connection.execute("DROP TABLE ops.fitbit_batch_intent")

    with pytest.raises(duckdb.CatalogException, match="fitbit_batch_intent"):
        DailyStateStore(warehouse, "self").finish(
            DailyState("self", last_daily_run=date(2026, 10, 3)),
            (
                CheckedWindow(first, NOW + timedelta(hours=1), "a"),
                CheckedWindow(second, NOW + timedelta(hours=1), "b"),
            ),
            retired_keys=("pending",),
        )

    assert warehouse.query_rows("SELECT fetched_at FROM ops.fitbit_coverage") == [(NOW,), (NOW,)]
    assert DailyStateStore(warehouse, "self").read().last_daily_run is None
    assert warehouse.connection_usable


def test_batch_candidates_require_one_exact_scope_and_no_subject_type_intent(warehouse):
    from personal_data_platform.sources.fitbit.writer import snapshot_skip_candidates

    first = Window("steps", NOW, NOW + timedelta(days=1))
    second = Window("heart-rate", NOW, NOW + timedelta(days=1))
    third = Window("steps", NOW + timedelta(days=1), NOW + timedelta(days=2))
    fourth = Window("active-zone-minutes", NOW, NOW + timedelta(days=1))
    coverage(warehouse, first, "a")
    coverage(warehouse, first, "other-hash", subject="other")
    coverage(warehouse, second, "b")
    coverage(warehouse, Window("steps", third.start, third.start + timedelta(hours=12)), "c")
    coverage(warehouse, Window("steps", third.start + timedelta(hours=12), third.end), "c")
    coverage(warehouse, fourth, "d", origin="zip")
    for subject, kind in (("self", "heart-rate"), ("other", "steps")):
        warehouse.connection.execute(
            "INSERT INTO ops.fitbit_raw_intent VALUES (?,?,?,?,?,?,?,?)",
            [
                subject + kind,
                0,
                subject,
                kind,
                NOW,
                NOW + timedelta(days=1),
                "pending-" + subject + kind,
                NOW,
            ],
        )
    connection = TracedConnection(warehouse.connection)
    warehouse.connection = connection

    assert snapshot_skip_candidates(warehouse, "self", (first, second, third, fourth, first)) == {
        first: ("api", "a"),
        fourth: ("zip", "d"),
    }
    assert len(connection.statements) == 1

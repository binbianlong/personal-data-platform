"""Fitbit transactions reject stale owners; Screen Time commits independently."""

import time
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.iphone_screen_time.batch import IPhoneScreenTimeBatch
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from personal_data_platform.storage.warehouse import WarehouseConnectionError, WarehouseLeaseError
from tests.screen_time_helpers import _raw, _record


@pytest.fixture
def warehouse():
    value = Warehouse(connect(WarehouseConfig(":memory:")))
    value.migrate()
    yield value
    value.close()


def test_screen_commit_is_independent_of_fitbit_lease_and_has_events_whitelist(warehouse):
    raw = _raw()
    batch = IPhoneScreenTimeBatch([_record(raw)])
    assert not warehouse.has_screen_time_events(source_id=raw.source_id)
    with pytest.raises(ValueError):
        warehouse.has_screen_time_events(source_id="location_point")
    assert warehouse.acquire_fitbit_lease("owner", lease_seconds=120)
    assert (
        warehouse.commit_screen_time_snapshot(
            raw, batch, invalidated_event_keys=(), deadline=time.monotonic() + 10
        )
        == 1
    )
    assert warehouse.has_screen_time_events(source_id=raw.source_id)
    warehouse.require_fitbit_lease("owner")
    assert warehouse.relation_names() & {"ops.job_lock", "ops.schema_migration"} == {
        "ops.job_lock",
        "ops.schema_migration",
    }
    assert not hasattr(warehouse, "mark_failed")


def test_release_does_not_delete_other_owner(warehouse):
    assert warehouse.acquire_fitbit_lease("new", lease_seconds=120)
    warehouse.release_fitbit_lease("old")
    warehouse.require_fitbit_lease("new")


@pytest.mark.parametrize("takeover_first", [False, True])
def test_independent_connections_fence_takeover(tmp_path, takeover_first):
    path = str(tmp_path / "fence.duckdb")
    first = Warehouse(connect(WarehouseConfig(path)))
    first.migrate()
    second = Warehouse(connect(WarehouseConfig(path)))
    try:
        assert first.acquire_fitbit_lease("old", lease_seconds=120)
        first.connection.execute("BEGIN")
        first.require_fitbit_lease("old")
        if takeover_first:
            second.connection.execute(
                "UPDATE ops.job_lock SET expires_at=?", [datetime.now(UTC) - timedelta(seconds=1)]
            )
            assert second.acquire_fitbit_lease("new", lease_seconds=120)
            with pytest.raises((duckdb.TransactionException, WarehouseLeaseError)):
                first._fence_fitbit_lease("old")
        else:
            first._fence_fitbit_lease("old")
            with pytest.raises(duckdb.TransactionException):
                second.connection.execute(
                    "UPDATE ops.job_lock SET owner_id='new', expires_at=expires_at + INTERVAL 1 SECOND"
                )
        first.connection.execute("ROLLBACK")
        if takeover_first:
            with pytest.raises(WarehouseLeaseError):
                first.require_fitbit_lease("old")
            first.release_fitbit_lease("old")
            second.require_fitbit_lease("new")
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("phase", ["BEGIN TRANSACTION", "COMMIT", "ROLLBACK"])
def test_uncertain_screen_transaction_stops_connection(warehouse, monkeypatch, phase):
    connection = warehouse.connection

    class Uncertain:
        def execute(self, sql, *args):
            if sql == phase:
                raise OSError("connection lost")
            return connection.execute(sql, *args)

    warehouse.connection = Uncertain()
    if phase == "ROLLBACK":
        monkeypatch.setattr(
            "personal_data_platform.sources.iphone_screen_time.motherduck.write_state",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad write")),
        )
    try:
        with pytest.raises(WarehouseConnectionError):
            warehouse.commit_screen_time_snapshot(
                _raw(),
                IPhoneScreenTimeBatch([_record(_raw())]),
                invalidated_event_keys=(),
                deadline=time.monotonic() + 10,
            )
        assert not warehouse.connection_usable
    finally:
        warehouse.connection = connection
        try:
            connection.execute("ROLLBACK")
        except duckdb.TransactionException:
            pass


@pytest.mark.parametrize("race", ["after_select", "before_commit"])
def test_competing_acquisition_defers_without_poisoning_connection(tmp_path, race):
    path = str(tmp_path / "acquire.duckdb")
    first = Warehouse(connect(WarehouseConfig(path)))
    first.migrate()
    second = Warehouse(connect(WarehouseConfig(path)))
    original = first.connection

    class CompetingAcquisition:
        def execute(self, sql, *args):
            if race == "before_commit" and sql == "COMMIT":
                assert second.acquire_fitbit_lease("second", lease_seconds=120)
            result = original.execute(sql, *args)
            if race == "after_select" and sql.startswith("SELECT owner_id, expires_at"):
                assert second.acquire_fitbit_lease("second", lease_seconds=120)
            return result

        def __getattr__(self, name):
            return getattr(original, name)

    first.connection = CompetingAcquisition()
    try:
        assert not first.acquire_fitbit_lease("first", lease_seconds=120)
        assert first.connection_usable
        second.require_fitbit_lease("second")
        # The losing transaction was aborted, and a new transaction is usable.
        first.connection = original
        second.release_fitbit_lease("second")
        assert first.acquire_fitbit_lease("retry", lease_seconds=120)
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("margin,allowed", [(1, False), (2, True)])
def test_fence_shortening_preserves_required_remaining_time(
    warehouse, monkeypatch, margin, allowed
):
    from personal_data_platform.storage import motherduck

    now = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    assert warehouse.acquire_fitbit_lease("owner", lease_seconds=120)
    expires = now + timedelta(seconds=1, microseconds=margin)
    warehouse.connection.execute("UPDATE ops.job_lock SET expires_at=?", [expires])
    monkeypatch.setattr(motherduck, "datetime", Clock)
    if allowed:
        with warehouse._transaction("fencing margin"):
            warehouse._fence_fitbit_lease("owner", remaining_seconds=1)
        result = warehouse.query_value("SELECT expires_at FROM ops.job_lock")
        assert result == expires - timedelta(microseconds=1)
        assert result > now + timedelta(seconds=1)
    else:
        with pytest.raises(WarehouseLeaseError):
            with warehouse._transaction("fencing margin"):
                warehouse._fence_fitbit_lease("owner", remaining_seconds=1)
        assert warehouse.query_value("SELECT expires_at FROM ops.job_lock") == expires


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, duckdb.TransactionException])
def test_unknown_lease_acquisition_commit_keeps_connection_unusable(
    warehouse, committed, error_type
):
    original = warehouse.connection
    statements_after_loss = []

    class LostResponse:
        lost = False

        def execute(self, sql, *args):
            if self.lost:
                statements_after_loss.append(sql)
            if sql == "COMMIT":
                if committed:
                    original.execute(sql, *args)
                self.lost = True
                raise error_type("lease commit response lost")
            return original.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(original, name)

    warehouse.connection = LostResponse()
    try:
        with pytest.raises(WarehouseConnectionError, match="commit outcome unknown"):
            warehouse.acquire_fitbit_lease("owner", lease_seconds=120)
        assert not warehouse.connection_usable
        warehouse.release_fitbit_lease("owner")
        assert statements_after_loss == []
    finally:
        warehouse.connection = original

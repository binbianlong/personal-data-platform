"""Shared writer transactions reject stale leases without operational receipts."""

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


def test_screen_commit_requires_live_owner_and_has_events_whitelist(warehouse):
    raw = _raw()
    batch = IPhoneScreenTimeBatch([_record(raw)])
    assert not warehouse.has_screen_time_events(source_id=raw.source_id)
    with pytest.raises(ValueError):
        warehouse.has_screen_time_events(source_id="location_point")
    assert warehouse.acquire_job_lock("loader", "owner", lease_seconds=120)
    with pytest.raises(WarehouseLeaseError):
        warehouse.commit_screen_time_snapshot(
            raw, batch, invalidated_event_keys=(), owner_id="old", deadline=time.monotonic() + 10
        )
    assert (
        warehouse.commit_screen_time_snapshot(
            raw, batch, invalidated_event_keys=(), owner_id="owner", deadline=time.monotonic() + 10
        )
        == 1
    )
    assert warehouse.has_screen_time_events(source_id=raw.source_id)
    assert warehouse.relation_names() & {"ops.job_lock", "ops.schema_migration"} == {
        "ops.job_lock",
        "ops.schema_migration",
    }
    assert not hasattr(warehouse, "mark_failed")


def test_release_does_not_delete_other_owner(warehouse):
    assert warehouse.acquire_job_lock("loader", "new", lease_seconds=120)
    warehouse.release_job_lock("loader", "old")
    warehouse.require_job_lock("new")


@pytest.mark.parametrize("takeover_first", [False, True])
def test_independent_connections_fence_takeover(tmp_path, takeover_first):
    path = str(tmp_path / "fence.duckdb")
    first = Warehouse(connect(WarehouseConfig(path)))
    first.migrate()
    second = Warehouse(connect(WarehouseConfig(path)))
    try:
        assert first.acquire_job_lock("loader", "old", lease_seconds=120)
        first.connection.execute("BEGIN")
        first.require_job_lock("old")
        if takeover_first:
            second.connection.execute(
                "UPDATE ops.job_lock SET expires_at=?", [datetime.now(UTC) - timedelta(seconds=1)]
            )
            assert second.acquire_job_lock("loader", "new", lease_seconds=120)
            with pytest.raises((duckdb.TransactionException, WarehouseLeaseError)):
                first.fence_job_lock("old")
        else:
            first.fence_job_lock("old")
            with pytest.raises(duckdb.TransactionException):
                second.connection.execute(
                    "UPDATE ops.job_lock SET owner_id='new', expires_at=expires_at + INTERVAL 1 SECOND"
                )
        first.connection.execute("ROLLBACK")
        if takeover_first:
            with pytest.raises(WarehouseLeaseError):
                first.require_job_lock("old")
            first.release_job_lock("loader", "old")
            second.require_job_lock("new")
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("phase", ["BEGIN TRANSACTION", "COMMIT", "ROLLBACK"])
def test_uncertain_screen_transaction_stops_connection(warehouse, monkeypatch, phase):
    assert warehouse.acquire_job_lock("loader", "owner", lease_seconds=120)
    connection = warehouse.connection

    class Uncertain:
        def execute(self, sql, *args):
            if sql == phase:
                raise OSError("connection lost")
            return connection.execute(sql, *args)

    warehouse.connection = Uncertain()
    if phase == "ROLLBACK":
        monkeypatch.setattr(
            warehouse,
            "fence_job_lock",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad write")),
        )
    try:
        with pytest.raises(WarehouseConnectionError):
            warehouse.commit_screen_time_snapshot(
                _raw(),
                IPhoneScreenTimeBatch([_record(_raw())]),
                invalidated_event_keys=(),
                owner_id="owner",
                deadline=time.monotonic() + 10,
            )
        assert not warehouse.connection_usable
    finally:
        warehouse.connection = connection
        try:
            connection.execute("ROLLBACK")
        except duckdb.TransactionException:
            pass


def test_later_migration_fences_same_lease_and_rolls_back_on_expiry(
    warehouse, tmp_path, monkeypatch
):
    import shutil

    from personal_data_platform.storage.migrations import DEFAULT_MIGRATIONS

    for sql in DEFAULT_MIGRATIONS.glob("*.sql"):
        shutil.copy(sql, tmp_path / sql.name)
    (tmp_path / "002_more.sql").write_text("CREATE TABLE base.extra(value INTEGER)")
    calls = []
    fence = warehouse.fence_job_lock

    def expire(owner, **kwargs):
        calls.append(owner)
        if len(calls) == 2:
            raise WarehouseLeaseError("expired during DDL")
        return fence(owner, **kwargs)

    monkeypatch.setattr(warehouse, "fence_job_lock", expire)
    with pytest.raises(WarehouseLeaseError):
        warehouse.migrate(tmp_path)
    assert len(calls) == 2
    assert "base.extra" not in warehouse.relation_names()
    assert warehouse.query_value("SELECT count(*) FROM ops.schema_migration") == 1


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
                assert second.acquire_job_lock("loader", "second", lease_seconds=120)
            result = original.execute(sql, *args)
            if race == "after_select" and sql.startswith("SELECT owner_id, expires_at"):
                assert second.acquire_job_lock("loader", "second", lease_seconds=120)
            return result

        def __getattr__(self, name):
            return getattr(original, name)

    first.connection = CompetingAcquisition()
    try:
        assert not first.acquire_job_lock("loader", "first", lease_seconds=120)
        assert first.connection_usable
        second.require_job_lock("second")
        # The losing transaction was aborted, and a new transaction is usable.
        first.connection = original
        second.release_job_lock("loader", "second")
        assert first.acquire_job_lock("loader", "retry", lease_seconds=120)
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

    assert warehouse.acquire_job_lock("loader", "owner", lease_seconds=120)
    expires = now + timedelta(seconds=1, microseconds=margin)
    warehouse.connection.execute("UPDATE ops.job_lock SET expires_at=?", [expires])
    monkeypatch.setattr(motherduck, "datetime", Clock)
    if allowed:
        with warehouse._transaction("fencing margin"):
            warehouse.fence_job_lock("owner", remaining_seconds=1)
        result = warehouse.query_value("SELECT expires_at FROM ops.job_lock")
        assert result == expires - timedelta(microseconds=1)
        assert result > now + timedelta(seconds=1)
    else:
        with pytest.raises(WarehouseLeaseError):
            with warehouse._transaction("fencing margin"):
                warehouse.fence_job_lock("owner", remaining_seconds=1)
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
            warehouse.acquire_job_lock("loader", "owner", lease_seconds=120)
        assert not warehouse.connection_usable
        warehouse.release_job_lock("loader", "owner")
        assert statements_after_loss == []
    finally:
        warehouse.connection = original

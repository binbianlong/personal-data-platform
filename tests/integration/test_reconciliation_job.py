from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from personal_data_platform.reconciliation.job import _relation_names, run_reconciliation
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


class _Repository:
    def list_raw(self, prefix):
        return []

    def list_scan_receipts(self):
        return [SimpleNamespace(device_key="a" * 64, completed_at=datetime.now(UTC))]


def _warehouse() -> Warehouse:
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    for relation in (
        "base.screen_time_transition",
        "base.screen_time_interval",
        "marts.daily_screen_time",
    ):
        warehouse.connection.execute(f"CREATE VIEW {relation} AS SELECT 1 AS value")
    return warehouse


def test_external_failure_preserves_previous_heartbeat_and_records_failure() -> None:
    warehouse = _warehouse()
    try:
        warehouse.publish_heartbeat("screen_time_reconciliation", "previous", {})

        def fail(_):
            raise RuntimeError("external monitor unreachable")

        result = run_reconciliation(_Repository(), warehouse, heartbeat=fail)

        assert not result.ok
        assert warehouse.query_value("SELECT status FROM ops.reconciliation_run") == "failed"
        assert warehouse.query_value("SELECT run_id FROM ops.heartbeat") == "previous"
    finally:
        warehouse.close()


def test_success_finalizes_the_pending_audit_and_warehouse_heartbeat() -> None:
    warehouse = _warehouse()
    try:
        published = []
        result = run_reconciliation(_Repository(), warehouse, heartbeat=published.append)

        assert result.ok
        assert warehouse.query_rows("SELECT run_id, status FROM ops.reconciliation_run") == [
            (result.run_id, "succeeded")
        ]
        assert warehouse.query_value("SELECT run_id FROM ops.heartbeat") == result.run_id
        assert len(published) == 1
    finally:
        warehouse.close()


def test_warehouse_write_failure_never_sends_external_success() -> None:
    warehouse = _warehouse()
    try:
        warehouse.connection.execute("DROP TABLE ops.heartbeat")
        published = []
        result = run_reconciliation(_Repository(), warehouse, heartbeat=published.append)

        assert not result.ok
        assert warehouse.query_value("SELECT status FROM ops.reconciliation_run") == "failed"
        assert published == []
    finally:
        warehouse.close()


def test_relation_inventory_ignores_other_attached_databases() -> None:
    warehouse = _warehouse()
    try:
        warehouse.connection.execute("DROP VIEW marts.daily_screen_time")
        warehouse.connection.execute("ATTACH ':memory:' AS unrelated")
        warehouse.connection.execute("CREATE SCHEMA unrelated.marts")
        warehouse.connection.execute(
            "CREATE VIEW unrelated.marts.daily_screen_time AS SELECT 1 AS value"
        )

        assert "marts.daily_screen_time" not in _relation_names(warehouse)
    finally:
        warehouse.close()

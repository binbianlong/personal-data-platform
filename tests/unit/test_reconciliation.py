from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from personal_data_platform.reconciliation.job import REQUIRED_RELATIONS, run_reconciliation

DEVICE_KEY = "a" * 64
NOW = datetime(2026, 8, 27, tzinfo=UTC)


class _Repository:
    def __init__(self, keys: list[str], *, receipt_at: datetime | None = NOW) -> None:
        self.keys = keys
        self.receipt_at = receipt_at

    def list_raw(self, prefix: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(key=key, device_key=DEVICE_KEY) for key in self.keys]

    def list_scan_receipts(self) -> list[SimpleNamespace]:
        if self.receipt_at is None:
            return []
        return [SimpleNamespace(device_key=DEVICE_KEY, completed_at=self.receipt_at)]


class _Warehouse:
    def __init__(
        self,
        loaded: set[str],
        *,
        failed: int = 0,
        relations: set[str] | None = None,
    ) -> None:
        self.loaded = loaded
        self.failed = failed
        self.relations = relations if relations is not None else set(REQUIRED_RELATIONS)
        self.reconciliations: list[object] = []
        self.heartbeats: list[tuple[str, str, dict[str, object]]] = []

    def succeeded_keys(self) -> set[str]:
        return set(self.loaded)

    def ingestion_counts(self) -> dict[str, int]:
        return {"failed": self.failed} if self.failed else {"succeeded": len(self.loaded)}

    def query_rows(self, sql: str) -> list[tuple[str]]:
        return [(relation,) for relation in self.relations]

    def query_value(self, sql: str) -> int:
        return 0

    def record_reconciliation(self, result: object) -> None:
        self.reconciliations.append(result)

    def publish_heartbeat(self, monitor_name: str, run_id: str, details: dict[str, object]) -> None:
        self.heartbeats.append((monitor_name, run_id, details))


def test_success_publishes_external_and_warehouse_heartbeat() -> None:
    warehouse = _Warehouse({"one", "two"})
    published: list[dict[str, object]] = []

    result = run_reconciliation(
        _Repository(["one", "two"]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=published.append,
        repair_missing=False,
        now=NOW,
    )

    assert result.ok
    assert len(published) == 1
    assert len(warehouse.heartbeats) == 1
    assert warehouse.reconciliations == [result]


def test_parity_failure_never_publishes_heartbeat() -> None:
    warehouse = _Warehouse({"one", "orphan"}, failed=1)
    published: list[dict[str, object]] = []

    result = run_reconciliation(
        _Repository(["one", "missing"]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=published.append,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.missing_object_count == 1
    assert result.orphaned_loaded_object_count == 1
    assert result.failed_object_count == 1
    assert published == []
    assert warehouse.heartbeats == []


def test_external_heartbeat_failure_marks_run_failed() -> None:
    warehouse = _Warehouse({"one"})

    def fail(_: dict[str, object]) -> None:
        raise RuntimeError("unreachable")

    result = run_reconciliation(
        _Repository(["one"]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=fail,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.details["heartbeat_error"] == "unreachable"
    assert warehouse.heartbeats == []


def test_stale_collector_receipt_blocks_success() -> None:
    warehouse = _Warehouse({"one"})
    published: list[dict[str, object]] = []

    result = run_reconciliation(
        _Repository(["one"], receipt_at=NOW - timedelta(hours=25)),
        warehouse,  # type: ignore[arg-type]
        heartbeat=published.append,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.stale_collector_count == 1
    assert published == []

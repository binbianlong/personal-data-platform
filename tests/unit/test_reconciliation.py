from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.loader.models import LoadSummary
from personal_data_platform.raw.models import RawObject
from personal_data_platform.reconciliation.job import (
    run_reconciliation,
    run_reconciliation_from_env,
)
from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.raw import ScreenTimeRawIdentity
from personal_data_platform.storage.motherduck import IngestionState

DEVICE_KEY = "a" * 64
NOW = datetime(2026, 8, 27, tzinfo=UTC)

REQUIRED_RELATIONS = get_source().required_relations


def _key(name: str) -> str:
    return ScreenTimeRawIdentity(
        device_key=DEVICE_KEY,
        stream="app-in-focus",
        segment_key=hashlib.sha256(name.encode()).hexdigest(),
        observed_at=NOW,
        sha256="c" * 64,
    ).object_key


class _Repository:
    def __init__(
        self,
        keys: list[str],
        *,
        receipt_at: datetime | None = NOW,
        manifest_at: datetime | None = NOW,
        created_at: dict[str, datetime | None] | None = None,
        generations: dict[str, int] | None = None,
    ) -> None:
        self.keys = keys
        self.receipt_at = receipt_at
        self.manifest_at = manifest_at
        self.created_at = created_at or {}
        self.generations = generations or {}

    def list_raw(self, prefix: str) -> list[RawObject]:
        return [
            get_source().parse_raw_key(
                key,
                storage_created_at=self.created_at.get(key, NOW - timedelta(days=1)),
                storage_generation=self.generations.get(key, 1),
            )
            for key in self.keys
            if key.startswith(prefix)
        ]

    def list_scan_receipts(self) -> list[SimpleNamespace]:
        if self.receipt_at is None:
            return []
        return [SimpleNamespace(device_key=DEVICE_KEY, completed_at=self.receipt_at)]

    def get_device_manifest(self) -> SimpleNamespace | None:
        if self.manifest_at is None:
            return None
        return SimpleNamespace(device_keys=(DEVICE_KEY,), completed_at=self.manifest_at)


class _Warehouse:
    def __init__(
        self,
        loaded: set[str],
        *,
        failed: int = 0,
        loading: int = 0,
        relations: set[str] | None = None,
        created_at: dict[str, datetime | None] | None = None,
        expired: set[str] | None = None,
    ) -> None:
        creation_times = created_at or {}
        expired_keys = expired or set()
        self.states = {
            key: IngestionState(
                object_key=key,
                status="succeeded",
                storage_created_at=creation_times.get(key, NOW - timedelta(days=1)),
                storage_generation=1,
                retention_expired_at=NOW if key in expired_keys else None,
            )
            for key in loaded
        }
        for index in range(failed):
            key = _key(f"failed-{index}")
            self.states[key] = IngestionState(
                object_key=key,
                status="failed",
                storage_created_at=creation_times.get(key, NOW - timedelta(days=1)),
                storage_generation=1,
                retention_expired_at=None,
            )
        for index in range(loading):
            key = _key(f"loading-{index}")
            self.states[key] = IngestionState(
                object_key=key,
                status="loading",
                storage_created_at=creation_times.get(key, NOW - timedelta(days=1)),
                storage_generation=1,
                retention_expired_at=None,
            )
        self.relations = relations if relations is not None else set(REQUIRED_RELATIONS)
        self.reconciliations: list[object] = []
        self.heartbeats: list[tuple[str, str, dict[str, object]]] = []
        self.connection = self
        self.recorded_statuses: list[str] = []

    def execute(self, sql: str) -> None:
        if sql == "BEGIN TRANSACTION":
            self._saved_heartbeats = list(self.heartbeats)
            self._saved_reconciliations = list(self.reconciliations)
            self._saved_states = dict(self.states)
        elif sql == "ROLLBACK":
            self.heartbeats = self._saved_heartbeats
            self.reconciliations = self._saved_reconciliations
            self.states = self._saved_states

    def succeeded_keys_for(self, raw_objects: list[SimpleNamespace]) -> set[str]:
        generations = {value.key: value.storage_generation for value in raw_objects}
        return {
            key
            for key, value in self.states.items()
            if value.status == "succeeded"
            and value.retention_expired_at is None
            and generations.get(key) == value.storage_generation
        }

    def active_ingestion_states(self, *, source_id: str, stream: str) -> dict[str, IngestionState]:
        assert (source_id, stream) == ("screen_time", "app-in-focus")
        return {
            key: value for key, value in self.states.items() if value.retention_expired_at is None
        }

    def retention_inventory_counts(self, *, source_id: str, stream: str) -> dict[str, int]:
        assert (source_id, stream) == ("screen_time", "app-in-focus")
        return {
            "total_object_count": len(self.states),
            "expired_object_count": sum(
                value.retention_expired_at is not None for value in self.states.values()
            ),
        }

    def mark_retention_expired(
        self, states: list[IngestionState], *, expired_at: datetime
    ) -> set[str]:
        expired: set[str] = set()
        for expected in states:
            current = self.states[expected.object_key]
            if current != expected:
                continue
            key = expected.object_key
            self.states[key] = IngestionState(
                object_key=current.object_key,
                status=current.status,
                storage_created_at=current.storage_created_at,
                storage_generation=current.storage_generation,
                retention_expired_at=expired_at,
            )
            expired.add(key)
        return expired

    def set_succeeded(self, keys: list[str]) -> None:
        for key in keys:
            self.states[key] = IngestionState(
                object_key=key,
                status="succeeded",
                storage_created_at=NOW - timedelta(days=1),
                storage_generation=1,
                retention_expired_at=None,
            )

    def query_rows(self, sql: str) -> list[tuple[str]]:
        return [(relation,) for relation in self.relations]

    def query_value(self, sql: str) -> int:
        return 0

    def record_reconciliation(self, result: object) -> None:
        self.recorded_statuses.append(result.status)
        self.reconciliations = [row for row in self.reconciliations if row.run_id != result.run_id]
        self.reconciliations.append(result)

    def publish_heartbeat(self, monitor_name: str, run_id: str, details: dict[str, object]) -> None:
        self.heartbeats.append((monitor_name, run_id, details))


def test_success_publishes_external_and_warehouse_heartbeat() -> None:
    warehouse = _Warehouse({_key("one"), _key("two")})
    published: list[dict[str, object]] = []

    result = run_reconciliation(
        _Repository([_key("one"), _key("two")]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=published.append,
        repair_missing=False,
        now=NOW,
    )

    assert result.ok
    assert len(published) == 1
    assert len(warehouse.heartbeats) == 1
    assert warehouse.reconciliations == [result]
    assert warehouse.recorded_statuses == ["running", "succeeded"]


def test_parity_failure_never_publishes_heartbeat() -> None:
    warehouse = _Warehouse({_key("one"), _key("orphan")}, failed=1)
    published: list[dict[str, object]] = []

    result = run_reconciliation(
        _Repository([_key("one"), _key("missing")]),
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
    warehouse = _Warehouse({_key("one")})

    def fail(_: dict[str, object]) -> None:
        raise RuntimeError("unreachable")

    result = run_reconciliation(
        _Repository([_key("one")]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=fail,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.details["heartbeat_error"] == "unreachable"
    assert warehouse.heartbeats == []
    assert warehouse.recorded_statuses == ["running", "failed"]


def test_stale_collector_receipt_blocks_success() -> None:
    warehouse = _Warehouse({_key("one")})
    published: list[dict[str, object]] = []

    result = run_reconciliation(
        _Repository([_key("one")], receipt_at=NOW - timedelta(hours=25)),
        warehouse,  # type: ignore[arg-type]
        heartbeat=published.append,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.stale_collector_count == 1
    assert published == []


def test_expected_lifecycle_expiry_is_persisted_and_does_not_block_heartbeat() -> None:
    warehouse = _Warehouse(
        {_key("expired")}, created_at={_key("expired"): NOW - timedelta(days=90)}
    )
    published: list[dict[str, object]] = []

    result = run_reconciliation(
        _Repository([]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=published.append,
        repair_missing=False,
        now=NOW,
    )

    assert result.ok
    assert result.loaded_object_count == 0
    assert result.orphaned_loaded_object_count == 0
    assert result.details["expired_object_count"] == 1
    assert result.details["newly_expired_object_count"] == 1
    assert warehouse.states[_key("expired")].retention_expired_at == NOW
    assert len(published) == 1


def test_retention_state_change_blocks_expiry_and_heartbeat(monkeypatch) -> None:
    warehouse = _Warehouse(
        {_key("expired")}, created_at={_key("expired"): NOW - timedelta(days=90)}
    )
    published: list[dict[str, object]] = []

    monkeypatch.setattr(warehouse, "mark_retention_expired", lambda *_, **__: set())

    result = run_reconciliation(
        _Repository([]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=published.append,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert "retention state changed" in result.details["retention_expiry_error"]
    assert result.details["newly_expired_object_count"] == 0
    assert warehouse.states[_key("expired")].retention_expired_at is None
    assert warehouse.heartbeats == []
    assert published == []


def test_premature_missing_raw_blocks_success() -> None:
    warehouse = _Warehouse(
        {_key("premature")}, created_at={_key("premature"): NOW - timedelta(days=89)}
    )

    result = run_reconciliation(
        _Repository([]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.orphaned_loaded_object_count == 1
    assert result.details["premature_missing_object_count"] == 1
    assert warehouse.states[_key("premature")].retention_expired_at is None


@pytest.mark.parametrize("status", ["failed", "loading"])
def test_uningested_raw_that_disappears_is_never_accepted_as_expired(status: str) -> None:
    warehouse = _Warehouse(set())
    warehouse.states[_key("unrecoverable")] = IngestionState(
        object_key=_key("unrecoverable"),
        status=status,
        storage_created_at=NOW - timedelta(days=90),
        storage_generation=1,
        retention_expired_at=None,
    )

    result = run_reconciliation(
        _Repository([]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.details["unrecoverable_uningested_object_count"] == 1


def test_unknown_storage_creation_time_fails_closed() -> None:
    warehouse = _Warehouse({_key("unknown")}, created_at={_key("unknown"): None})
    repository = _Repository([])

    result = run_reconciliation(
        repository,
        warehouse,  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.details["unknown_creation_time_object_count"] == 1


def test_lifecycle_lag_is_allowed_until_the_third_day() -> None:
    created_at = NOW - timedelta(days=92)
    warehouse = _Warehouse({_key("lagging")}, created_at={_key("lagging"): created_at})

    result = run_reconciliation(
        _Repository([_key("lagging")], created_at={_key("lagging"): created_at}),
        warehouse,  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert result.ok
    assert result.details["lifecycle_lag_object_count"] == 1
    assert result.details["overdue_deletion_object_count"] == 0


def test_raw_still_live_on_day_93_is_an_overdue_deletion() -> None:
    created_at = NOW - timedelta(days=93)
    warehouse = _Warehouse({_key("overdue")}, created_at={_key("overdue"): created_at})

    result = run_reconciliation(
        _Repository([_key("overdue")], created_at={_key("overdue"): created_at}),
        warehouse,  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.details["overdue_deletion_object_count"] == 1


def test_expected_expiry_is_not_persisted_when_another_audit_check_fails() -> None:
    warehouse = _Warehouse(
        {_key("expired")},
        created_at={_key("expired"): NOW - timedelta(days=91)},
    )

    result = run_reconciliation(
        _Repository([], receipt_at=None),
        warehouse,  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.details["expected_expiry_candidate_count"] == 1
    assert result.details["newly_expired_object_count"] == 0
    assert warehouse.states[_key("expired")].retention_expired_at is None


def test_historical_expired_rows_are_not_reprocessed() -> None:
    warehouse = _Warehouse({_key("historical")}, expired={_key("historical")})

    result = run_reconciliation(
        _Repository([]),
        warehouse,  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert result.ok
    assert result.details["total_object_count"] == 1
    assert result.details["expired_object_count"] == 1
    assert result.details["expected_expiry_candidate_count"] == 0


def test_expired_key_seen_live_again_requires_loader_verification() -> None:
    warehouse = _Warehouse(
        {_key("recreated")},
        created_at={_key("recreated"): NOW - timedelta(days=90)},
        expired={_key("recreated")},
    )
    recreated_at = NOW - timedelta(hours=1)

    result = run_reconciliation(
        _Repository([_key("recreated")], created_at={_key("recreated"): recreated_at}),
        warehouse,  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.missing_object_count == 1
    assert warehouse.states[_key("recreated")].retention_expired_at == NOW


def test_missing_device_manifest_blocks_success() -> None:
    result = run_reconciliation(
        _Repository([], manifest_at=None),
        _Warehouse(set()),  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.details["collector_manifest_present"] is False


def test_manifest_device_without_a_receipt_blocks_success() -> None:
    missing_device_key = "b" * 64

    class Repository(_Repository):
        def get_device_manifest(self) -> SimpleNamespace:
            return SimpleNamespace(
                device_keys=(DEVICE_KEY, missing_device_key),
                completed_at=NOW,
            )

    result = run_reconciliation(
        Repository([_key("one")]),
        _Warehouse({_key("one")}),  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert not result.ok
    assert result.missing_collector_receipt_count == 1
    assert result.details["missing_collector_receipt_devices"] == [missing_device_key]


def test_decommissioned_device_raw_does_not_require_a_fresh_receipt() -> None:
    active_device_key = "b" * 64

    class Repository(_Repository):
        def get_device_manifest(self) -> SimpleNamespace:
            return SimpleNamespace(device_keys=(active_device_key,), completed_at=NOW)

        def list_scan_receipts(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(device_key=active_device_key, completed_at=NOW),
                SimpleNamespace(
                    device_key=DEVICE_KEY,
                    completed_at=NOW - timedelta(days=7),
                ),
            ]

    result = run_reconciliation(
        Repository([_key("retained-from-decommissioned-device")]),
        _Warehouse({_key("retained-from-decommissioned-device")}),  # type: ignore[arg-type]
        heartbeat=lambda _: None,
        repair_missing=False,
        now=NOW,
    )

    assert result.ok
    assert result.stale_collector_count == 0
    assert result.details["retained_inactive_device_count"] == 1


def test_repair_rechecks_objects_uploaded_after_initial_inventory(monkeypatch) -> None:
    repository = _Repository([_key("original")])
    warehouse = _Warehouse(set())
    published: list[dict[str, object]] = []

    def repair(repository, warehouse, *, source, prefix):
        assert (source.source_id, source.stream) == ("screen_time", "app-in-focus")
        repository.keys.append(_key("arrived-during-repair"))
        warehouse.set_succeeded(repository.keys)
        # This later upload has not been loaded and belongs to the next audit.
        repository.keys.append(_key("arrived-after-repair"))
        return LoadSummary(discovered=2, skipped=0, succeeded=2, failed=0, records=2)

    monkeypatch.setattr("personal_data_platform.reconciliation.job.run_loader", repair)

    result = run_reconciliation(repository, warehouse, heartbeat=published.append, now=NOW)

    assert result.ok
    assert result.raw_object_count == result.loaded_object_count == 2
    assert result.missing_object_count == result.orphaned_loaded_object_count == 0
    assert len(published) == 1


def test_external_heartbeat_follows_pending_audit_and_warehouse_write() -> None:
    warehouse = _Warehouse({_key("one")})

    def publish(_: dict[str, object]) -> None:
        assert warehouse.recorded_statuses == ["running"]
        assert len(warehouse.heartbeats) == 1

    result = run_reconciliation(_Repository([_key("one")]), warehouse, heartbeat=publish, now=NOW)

    assert result.ok


def test_audit_write_failure_blocks_external_heartbeat(monkeypatch) -> None:
    warehouse = _Warehouse({_key("one")})
    published: list[dict[str, object]] = []

    def fail(_):
        raise RuntimeError("audit write unavailable")

    monkeypatch.setattr(warehouse, "record_reconciliation", fail)
    with pytest.raises(RuntimeError, match="audit write unavailable"):
        run_reconciliation(
            _Repository([_key("one")]), warehouse, heartbeat=published.append, now=NOW
        )

    assert published == []
    assert warehouse.heartbeats == []


def test_warehouse_heartbeat_failure_blocks_external_heartbeat(monkeypatch) -> None:
    warehouse = _Warehouse({_key("one")})
    published: list[dict[str, object]] = []

    def fail(*_):
        raise RuntimeError("warehouse write unavailable")

    monkeypatch.setattr(warehouse, "publish_heartbeat", fail)
    result = run_reconciliation(
        _Repository([_key("one")]), warehouse, heartbeat=published.append, now=NOW
    )

    assert not result.ok
    assert result.details["warehouse_heartbeat_error"] == "warehouse write unavailable"
    assert warehouse.reconciliations == [result]
    assert warehouse.heartbeats == []
    assert published == []


def test_reconciliation_rejects_partial_inventory_before_auditing_warehouse():
    source = get_source()
    with pytest.raises(ValueError, match="every canonical prefix"):
        run_reconciliation(
            _Repository([]),
            _Warehouse(set()),
            source=source,
            prefix=source.raw_prefixes[0] + DEVICE_KEY + "/",
            heartbeat=lambda _: None,
        )


@pytest.mark.parametrize(
    ("name", "value"), [("PDP_RAW_RETENTION_DAYS", "7"), ("PDP_LIFECYCLE_GRACE_DAYS", "0")]
)
def test_reconciliation_rejects_deployment_retention_drift_before_cloud_access(
    monkeypatch, name, value
):
    monkeypatch.setenv("RECONCILIATION_HEARTBEAT_URL", "https://heartbeat.invalid/ping")
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "personal_data_platform.sources.screen_time.adapter.ScreenTimeSource.repository_from_env",
        lambda _: pytest.fail("repository must not open with mismatched retention"),
    )

    with pytest.raises(ValueError, match=name):
        run_reconciliation_from_env()

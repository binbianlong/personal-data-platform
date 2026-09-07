from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.raw.models import RawObject
from personal_data_platform.reconciliation.job import _relation_names, run_reconciliation
from personal_data_platform.sources.contracts import SourceHealth
from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.raw import ScreenTimeRawIdentity
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


class _Repository:
    def list_raw(self, prefix):
        return []

    def list_scan_receipts(self):
        return [SimpleNamespace(device_key="a" * 64, completed_at=datetime.now(UTC))]

    def get_device_manifest(self):
        return SimpleNamespace(device_keys=("a" * 64,), completed_at=datetime.now(UTC))


def _raw(storage_created_at: datetime) -> RawObject:
    identity = ScreenTimeRawIdentity(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        observed_at=storage_created_at,
        sha256="c" * 64,
    )
    return get_source().parse_raw_key(
        identity.object_key, storage_created_at=storage_created_at, storage_generation=1
    )


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


def test_successful_audit_persists_expected_lifecycle_expiry() -> None:
    warehouse = _warehouse()
    try:
        now = datetime.now(UTC)
        raw = _raw(now - timedelta(days=90))
        warehouse.load_object(raw, byte_size=0, batch=ScreenTimeBatch([]))

        result = run_reconciliation(_Repository(), warehouse, heartbeat=lambda _: None, now=now)

        assert result.ok
        assert result.loaded_object_count == 0
        assert result.details["newly_expired_object_count"] == 1
        assert (
            warehouse.query_value(
                "SELECT retention_expired_at FROM ops.ingestion_metadata WHERE object_key = ?",
                [raw.key],
            )
            == now
        )
    finally:
        warehouse.close()


def test_failed_heartbeat_rolls_back_expected_expiry() -> None:
    warehouse = _warehouse()
    try:
        now = datetime.now(UTC)
        raw = _raw(now - timedelta(days=91))
        warehouse.load_object(raw, byte_size=0, batch=ScreenTimeBatch([]))

        def fail(_):
            raise RuntimeError("external monitor unreachable")

        result = run_reconciliation(_Repository(), warehouse, heartbeat=fail, now=now)

        assert not result.ok
        assert result.details["newly_expired_object_count"] == 0
        assert (
            warehouse.query_value(
                "SELECT retention_expired_at FROM ops.ingestion_metadata WHERE object_key = ?",
                [raw.key],
            )
            is None
        )
    finally:
        warehouse.close()


class _EmptyBatch:
    parser_version = "synthetic-1"
    record_count = 0

    def write(self, connection, raw, *, byte_size, loaded_at):
        pass


class _SyntheticSource:
    schema_versions = (1, 2)
    raw_suffixes = (".json.gz",)
    required_relations = ()
    retention_days = 7
    lifecycle_grace_days = 2
    dbt_selector = "tag:synthetic"

    def __init__(self, source_id: str, stream: str, observations: list[RawObject]):
        self.source_id = source_id
        self.stream = stream
        self.raw_prefixes = tuple(f"raw/{source_id}/v{version}/{stream}/" for version in (1, 2))
        self.monitor_name = f"{source_id}_{stream}_reconciliation"
        self.observations = {raw.key: raw for raw in observations}
        self.audited: list[RawObject] = []

    def validate_raw_key(self, key):
        if not key.startswith(self.raw_prefixes) or not key.endswith(self.raw_suffixes):
            raise ValueError("invalid synthetic Raw key")

    def parse_raw_key(self, key, *, storage_created_at, storage_generation):
        from dataclasses import replace

        return replace(
            self.observations[key],
            storage_created_at=storage_created_at,
            storage_generation=storage_generation,
        )

    def decode(self, raw, payload):
        assert payload == b"payload"
        return _EmptyBatch()

    def legacy_scope(self, raw):
        return None

    def audit(self, repository, observations, now):
        self.audited = list(observations)
        return SourceHealth(ok=True, details={"synthetic_health": "fresh"})


def _synthetic_raw(source_id, stream, version, created_at, *, name="same-scope"):
    import hashlib

    return RawObject(
        key=f"raw/{source_id}/v{version}/{stream}/{name}.json.gz",
        source_id=source_id,
        schema_version=version,
        subject_key="same-subject",
        stream=stream,
        logical_key=name,
        observed_at=created_at,
        sha256=hashlib.sha256(b"payload").hexdigest(),
        storage_created_at=created_at,
        storage_generation=version,
    )


@pytest.mark.parametrize("other_scope", [("other_source", "metrics"), ("synthetic", "sleep")])
def test_reconciliation_isolates_source_and_stream_and_repairs_all_supported_schemas(other_scope):
    import gzip

    now = datetime.now(UTC)
    raw_v1 = _synthetic_raw("synthetic", "metrics", 1, now)
    raw_v2 = _synthetic_raw("synthetic", "metrics", 2, now)
    expired = _synthetic_raw("synthetic", "metrics", 1, now - timedelta(days=7), name="expired")
    other_failed = _synthetic_raw(*other_scope, 1, now - timedelta(days=20), name="failed")
    other_old = _synthetic_raw(*other_scope, 1, now - timedelta(days=20), name="old")
    source = _SyntheticSource("synthetic", "metrics", [raw_v1, raw_v2])
    reads = []

    class Repository:
        def list_raw(self, prefix):
            return [raw for raw in (raw_v1, raw_v2) if raw.key.startswith(prefix)]

        def get_raw(self, key, *, generation):
            reads.append((key, generation))
            return gzip.compress(b"payload")

    warehouse = _warehouse()
    try:
        warehouse.load_object(raw_v1, byte_size=0, batch=_EmptyBatch())
        warehouse.load_object(expired, byte_size=0, batch=_EmptyBatch())
        warehouse.load_object(other_old, byte_size=0, batch=_EmptyBatch())
        warehouse.mark_failed(other_failed, byte_size=0, error=RuntimeError("unrelated failure"))
        result = run_reconciliation(
            Repository(), warehouse, source=source, heartbeat=lambda _: None, now=now
        )

        assert result.ok
        assert result.failed_object_count == 0
        assert result.raw_object_count == result.loaded_object_count == 2
        assert result.details["total_object_count"] == 3
        assert result.details["expired_object_count"] == 1
        assert result.details["synthetic_health"] == "fresh"
        assert result.collector_receipt_count == 0
        assert source.audited == sorted(
            (raw_v1, raw_v2), key=lambda raw: (raw.observed_at, raw.key)
        )
        assert reads == [(raw_v2.key, raw_v2.storage_generation)]
        assert (
            warehouse.query_value("SELECT monitor_name FROM ops.heartbeat") == source.monitor_name
        )
        assert warehouse.query_rows(
            "SELECT status, retention_expired_at FROM ops.ingestion_metadata "
            "WHERE source_id = ? AND source_stream = ? ORDER BY object_key",
            list(other_scope),
        ) == [("failed", None), ("succeeded", None)]
    finally:
        warehouse.close()

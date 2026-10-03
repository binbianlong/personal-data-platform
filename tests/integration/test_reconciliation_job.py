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
from tests.sql_helpers import TracedConnection


@pytest.fixture
def scheduled_warehouse(monkeypatch):
    from personal_data_platform.reconciliation import job

    warehouse = _warehouse()
    connection = TracedConnection(warehouse.connection)
    warehouse.connection = connection
    monkeypatch.setenv("PDP_RECONCILIATION_MONITORING_MODE", "cloud_monitoring")
    monkeypatch.delenv("RECONCILIATION_HEARTBEAT_URL", raising=False)
    monkeypatch.delenv("PDP_FITBIT_DAILY_ENABLED", raising=False)
    monkeypatch.setenv("MOTHERDUCK_DATABASE", ":memory:")
    monkeypatch.setattr(job, "connect", lambda _config: connection)
    monkeypatch.setattr(job, "Warehouse", lambda _connection: warehouse)
    monkeypatch.setattr(warehouse, "close", lambda: None)
    yield warehouse, connection
    connection.close()


def test_scheduled_screen_time_checks_each_shared_relation_once(scheduled_warehouse, monkeypatch):
    from personal_data_platform.reconciliation import job
    from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource

    warehouse, connection = scheduled_warehouse
    monkeypatch.setattr(ScreenTimeSource, "repository_from_env", lambda _source: _Repository())

    assert job.run_reconciliation_from_env(source_id="screen_time", all_streams=True) == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.reconciliation_run") == 2
    assert warehouse.query_value("SELECT count(*) FROM ops.heartbeat") == 2
    for relation in get_source().required_relations:
        assert connection.statements.count(f"SELECT count(*) FROM {relation}") == 1


def test_missing_shared_view_blocks_both_stream_heartbeats(scheduled_warehouse, monkeypatch):
    import json

    from personal_data_platform.reconciliation import job
    from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource

    warehouse, _ = scheduled_warehouse
    warehouse.connection.execute("DROP VIEW marts.daily_screen_time_total")
    monkeypatch.setattr(ScreenTimeSource, "repository_from_env", lambda _source: _Repository())

    assert job.run_reconciliation_from_env(source_id="screen_time", all_streams=True) == 1
    assert warehouse.query_value("SELECT count(*) FROM ops.heartbeat") == 0
    results = warehouse.query_rows("SELECT status, details FROM ops.reconciliation_run")
    assert len(results) == 2
    for status, details in results:
        assert status == "failed"
        assert json.loads(details)["missing_relations"] == ["marts.daily_screen_time_total"]


@pytest.mark.parametrize("initially_healthy", [True, False])
def test_prepared_audit_survives_a_later_unusable_connection(
    scheduled_warehouse, monkeypatch, initially_healthy
):
    import gzip
    import json

    from personal_data_platform.reconciliation import job
    from personal_data_platform.storage.motherduck import WarehouseConnectionError

    warehouse, _ = scheduled_warehouse
    expired = _synthetic_raw("synthetic", "first", 1, datetime.now(UTC) - timedelta(days=8))
    pending = _synthetic_raw("synthetic", "second", 1, datetime.now(UTC))
    warehouse.load_object(expired, byte_size=0, batch=_EmptyBatch())

    class Source(_SyntheticSource):
        def repository_from_env(self):
            observations = [] if self.stream == "first" else [pending]

            class Repository:
                def list_raw(self, prefix):
                    return [raw for raw in observations if raw.key.startswith(prefix)]

                def get_raw(self, key, *, generation):
                    return gzip.compress(b"payload")

            return Repository()

        def audit(self, repository, observations, now):
            return SourceHealth(
                ok=initially_healthy, details={"collector_fresh": initially_healthy}
            )

    sources = (Source("synthetic", "first", [expired]), Source("synthetic", "second", [pending]))
    monkeypatch.setattr(job, "get_sources", lambda *_args, **_kwargs: sources)

    def fail_commit(raw, *, byte_size, batch):
        warehouse.connection_usable = False
        raise WarehouseConnectionError("commit outcome unknown; reopen warehouse")

    monkeypatch.setattr(warehouse, "load_object", fail_commit)

    assert job.run_reconciliation_from_env(source_id="synthetic", all_streams=True) == 1
    assert not warehouse.connection_usable
    assert warehouse.query_value("SELECT count(*) FROM ops.heartbeat") == 0
    results = warehouse.query_rows("SELECT status, details FROM ops.reconciliation_run")
    assert len(results) == 1
    status, details = results[0]
    assert status == ("running" if initially_healthy else "failed")
    details = json.loads(details)
    assert details["stream"] == "first"
    assert details["expired_object_count"] == details["newly_expired_object_count"] == 0
    assert details["expected_expiry_candidate_count"] == 1
    assert (
        warehouse.query_value(
            "SELECT retention_expired_at FROM ops.ingestion_metadata WHERE object_key = ?",
            [expired.key],
        )
        is None
    )


def test_shared_inventory_error_records_failed_audits(scheduled_warehouse, monkeypatch):
    import json

    from personal_data_platform.reconciliation import job
    from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource

    warehouse, _ = scheduled_warehouse
    monkeypatch.setattr(ScreenTimeSource, "repository_from_env", lambda _source: _Repository())
    query_rows = warehouse.query_rows

    def fail_inventory(sql, parameters=None):
        if "information_schema.tables" in sql:
            raise OSError("relation inventory temporarily unavailable")
        return query_rows(sql, parameters)

    monkeypatch.setattr(warehouse, "query_rows", fail_inventory)

    assert job.run_reconciliation_from_env(source_id="screen_time", all_streams=True) == 1
    assert warehouse.connection_usable
    assert warehouse.query_value("SELECT count(*) FROM ops.heartbeat") == 0
    results = warehouse.query_rows("SELECT status, details FROM ops.reconciliation_run")
    assert len(results) == 2
    for status, details in results:
        assert status == "failed"
        assert (
            json.loads(details)["relation_checks_error"]
            == "relation inventory temporarily unavailable"
        )


@pytest.mark.parametrize("second_value", ["2", "invalid"])
def test_shared_relation_checks_observe_both_repairs_before_success(
    scheduled_warehouse, monkeypatch, second_value
):
    import gzip

    from personal_data_platform.reconciliation import job

    warehouse, connection = scheduled_warehouse
    connection.execute("CREATE TABLE ops.reconciliation_probe (value VARCHAR)")
    connection.execute(
        "CREATE VIEW marts.shared_probe AS SELECT value FROM ops.reconciliation_probe "
        "WHERE CAST(value AS INTEGER) > 0"
    )

    class ProbeBatch(_EmptyBatch):
        def __init__(self, value):
            self.value = value

        def write(self, connection, raw, *, byte_size, loaded_at):
            connection.execute("INSERT INTO ops.reconciliation_probe VALUES (?)", [self.value])

    class Source(_SyntheticSource):
        required_relations = ("marts.shared_probe",)

        def __init__(self, stream, value):
            self.raw = _synthetic_raw("synthetic", stream, 1, datetime.now(UTC))
            super().__init__("synthetic", stream, [self.raw])
            self.value = value

        def repository_from_env(self):
            raw = self.raw

            class Repository:
                def list_raw(self, prefix):
                    return [raw] if raw.key.startswith(prefix) else []

                def get_raw(self, key, *, generation):
                    return gzip.compress(b"payload")

            return Repository()

        def decode(self, raw, payload):
            return ProbeBatch(self.value)

    sources = (Source("first", "1"), Source("second", second_value))
    monkeypatch.setattr(job, "get_sources", lambda *_args, **_kwargs: sources)

    failed = second_value == "invalid"
    assert job.run_reconciliation_from_env(source_id="synthetic", all_streams=True) == int(failed)
    assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 2
    assert warehouse.query_rows("SELECT status FROM ops.reconciliation_run") == [
        ("failed" if failed else "succeeded",),
        ("failed" if failed else "succeeded",),
    ]
    assert warehouse.query_value("SELECT count(*) FROM ops.heartbeat") == (0 if failed else 2)
    checks = [
        index
        for index, sql in enumerate(connection.statements)
        if sql == "SELECT count(*) FROM marts.shared_probe"
    ]
    repairs = [
        index
        for index, sql in enumerate(connection.statements)
        if sql == "INSERT INTO ops.reconciliation_probe VALUES (?)"
    ]
    assert len(checks) == 1 and checks[0] > max(repairs)


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
        "marts.daily_screen_time_total",
    ):
        warehouse.connection.execute(f"CREATE OR REPLACE VIEW {relation} AS SELECT 1 AS value")
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


def test_missing_daily_total_view_fails_reconciliation() -> None:
    warehouse = _warehouse()
    try:
        warehouse.connection.execute("DROP VIEW marts.daily_screen_time_total")
        published = []

        result = run_reconciliation(_Repository(), warehouse, heartbeat=published.append)

        assert not result.ok
        assert result.missing_relations == ("marts.daily_screen_time_total",)
        assert published == []
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
    parser_version = _EmptyBatch.parser_version
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
    listings = []

    class Repository:
        def list_raw(self, prefix):
            listings.append(prefix)
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
        # Expired warehouse keys still require a fresh inventory check.
        assert listings == list(source.raw_prefixes) * 2
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


@pytest.mark.parametrize("repair_mode", ["success", "disabled", "failure", "stale"])
def test_reconciliation_requires_current_parser_before_success(repair_mode):
    import gzip
    from dataclasses import replace

    from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource
    from personal_data_platform.sources.screen_time.raw import (
        CollectorDeviceManifest,
        CollectorScanReceipt,
    )
    from tests.screen_time_helpers import NOW, Repository, event, segb

    class LiveRepository(Repository):
        def list_scan_receipts(self):
            return [CollectorScanReceipt("a" * 64, NOW, 1)]

        def get_device_manifest(self):
            return CollectorDeviceManifest(("a" * 64,), NOW)

    class CorrectedSource(ScreenTimeSource):
        parser_version = "corrected-parser"

        def decode(self, raw, payload):
            if repair_mode == "failure":
                raise ValueError("cannot decode with corrected parser")
            batch = super().decode(raw, payload)
            if repair_mode == "stale":
                return batch
            return replace(
                batch,
                records=[
                    replace(record, parser_version=self.parser_version, app_version="corrected")
                    for record in batch.records
                ],
            )

    repository = LiveRepository()
    raw = repository.add("100", segb(event("app.versioned"))[0])
    warehouse = _warehouse()
    published = []
    try:
        old_batch = ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))
        warehouse.load_object(raw, byte_size=1, batch=old_batch)
        result = run_reconciliation(
            repository,
            warehouse,
            source=CorrectedSource(),
            heartbeat=published.append,
            repair_missing=repair_mode != "disabled",
            now=NOW,
        )
        assert result.details["missing_before_repair"] == 1
        if repair_mode == "success":
            assert result.ok
            assert result.missing_object_count == 0
            assert len(published) == 1
            assert warehouse.query_rows(
                "SELECT parser_version, app_version FROM base.screen_time_event"
            ) == [("corrected-parser", "corrected")]
            assert (
                warehouse.query_value("SELECT parser_version FROM ops.ingestion_metadata")
                == "corrected-parser"
            )
        else:
            assert not result.ok
            assert result.missing_object_count == 1
            assert published == []
            assert warehouse.query_value("SELECT count(*) FROM ops.heartbeat") == 0
        if repair_mode == "disabled":
            assert result.details["repair_summary"] is None
        elif repair_mode == "failure":
            assert result.failed_object_count == 1
        elif repair_mode == "stale":
            assert result.details["repair_summary"]["succeeded"] == 1
    finally:
        warehouse.close()

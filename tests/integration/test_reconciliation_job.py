from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.biome.collector_state import CollectorScan
from personal_data_platform.raw.models import RawObject
from personal_data_platform.reconciliation.job import _relation_names, run_reconciliation
from personal_data_platform.sources.contracts import SourceHealth
from personal_data_platform.sources.iphone_screen_time.raw import ScreenTimeRawIdentity
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


class _Repository:
    def list_raw(self, prefix):
        return []

    def last_successful_scan(self):
        now = datetime.now(UTC)
        return CollectorScan(now, ("a" * 64,), {"a" * 64: now})


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
    warehouse = _SyntheticWarehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    for relation in (
        "base.iphone_screen_time_transition",
        "base.iphone_screen_time_interval",
        "marts.daily_iphone_screen_time",
        "marts.daily_iphone_screen_time_total",
        "base.mac_screen_time_transition",
        "base.mac_screen_time_interval",
        "marts.daily_mac_screen_time",
        "marts.daily_mac_screen_time_total",
    ):
        warehouse.connection.execute(f"CREATE OR REPLACE VIEW {relation} AS SELECT 1 AS value")
    return warehouse


@pytest.mark.parametrize("missing_view", [False, True])
def test_raw_audit_returns_result_without_changing_collector_success(missing_view) -> None:
    warehouse = _warehouse()
    try:
        warehouse.publish_heartbeat("iphone_screen_time_reconciliation", "collector", {})
        previous = warehouse.query_rows("SELECT * FROM ops.heartbeat")
        if missing_view:
            warehouse.connection.execute("DROP VIEW marts.daily_iphone_screen_time_total")
        result = run_reconciliation(_Repository(), warehouse)

        assert result.ok is not missing_view
        assert warehouse.query_rows("SELECT * FROM ops.heartbeat") == previous
        assert warehouse.query_rows("SELECT * FROM ops.job_run") == []
        assert "ops.reconciliation_run" not in _relation_names(warehouse)
    finally:
        warehouse.close()


def test_missing_daily_total_view_fails_reconciliation() -> None:
    warehouse = _warehouse()
    try:
        warehouse.connection.execute("DROP VIEW marts.daily_iphone_screen_time_total")

        result = run_reconciliation(_Repository(), warehouse)

        assert not result.ok
        assert result.missing_relations == ("marts.daily_iphone_screen_time_total",)
    finally:
        warehouse.close()


def test_relation_inventory_ignores_other_attached_databases() -> None:
    warehouse = _warehouse()
    try:
        warehouse.connection.execute("DROP VIEW marts.daily_iphone_screen_time")
        warehouse.connection.execute("ATTACH ':memory:' AS unrelated")
        warehouse.connection.execute("CREATE SCHEMA unrelated.marts")
        warehouse.connection.execute(
            "CREATE VIEW unrelated.marts.daily_iphone_screen_time AS SELECT 1 AS value"
        )

        assert "marts.daily_iphone_screen_time" not in _relation_names(warehouse)
    finally:
        warehouse.close()


class _EmptyBatch:
    parser_version = "synthetic-1"
    record_count = 0


class _SyntheticWarehouse(Warehouse):
    def _write_batch(self, raw, batch, *, loaded_at):
        if not isinstance(batch, _EmptyBatch):
            return super()._write_batch(raw, batch, loaded_at=loaded_at)


class _SyntheticSource:
    parser_version = _EmptyBatch.parser_version
    schema_versions = (1, 2)
    raw_suffixes = (".json.gz",)
    required_relations = ()
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
        warehouse.load_object(other_old, byte_size=0, batch=_EmptyBatch())
        warehouse.mark_failed(other_failed, byte_size=0, error=RuntimeError("unrelated failure"))
        result = run_reconciliation(Repository(), warehouse, source=source, now=now)

        assert result.ok
        assert result.failed_object_count == 0
        assert result.raw_object_count == result.loaded_object_count == 2
        assert result.details["total_object_count"] == 2
        assert result.details["synthetic_health"] == "fresh"
        assert result.collector_device_count == 0
        assert source.audited == sorted(
            (raw_v1, raw_v2), key=lambda raw: (raw.observed_at, raw.key)
        )
        assert reads == [(raw_v2.key, raw_v2.storage_generation)]
        assert warehouse.query_rows("SELECT * FROM ops.heartbeat") == []
        assert warehouse.query_rows(
            "SELECT status FROM ops.ingestion_metadata "
            "WHERE source_id = ? AND source_stream = ? ORDER BY object_key",
            list(other_scope),
        ) == [("failed",), ("succeeded",)]
    finally:
        warehouse.close()


@pytest.mark.parametrize("repair_mode", ["success", "disabled", "failure", "stale"])
def test_reconciliation_requires_current_parser_before_success(repair_mode):
    import gzip
    from dataclasses import replace

    from personal_data_platform.sources.iphone_screen_time.adapter import (
        IPhoneScreenTimeSource as ScreenTimeSource,
    )
    from tests.screen_time_helpers import NOW, Repository, event, segb

    class LiveRepository(Repository):
        def last_successful_scan(self):
            return CollectorScan(NOW, ("a" * 64,), {"a" * 64: NOW})

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
    try:
        old_batch = ScreenTimeSource().decode(raw, gzip.decompress(repository.objects[raw.key][1]))
        warehouse.load_object(raw, byte_size=1, batch=old_batch)
        result = run_reconciliation(
            repository,
            warehouse,
            source=CorrectedSource(),
            repair_missing=repair_mode != "disabled",
            now=NOW,
        )
        assert result.details["missing_before_repair"] == 1
        if repair_mode == "success":
            assert result.ok
            assert result.missing_object_count == 0
            assert warehouse.query_rows(
                "SELECT parser_version FROM base.iphone_screen_time_event"
            ) == [("corrected-parser",)]
            assert warehouse.query_rows(
                "SELECT parser_version, app_version FROM ops.iphone_screen_time_record"
            ) == [("corrected-parser", "corrected")]
            assert (
                warehouse.query_value("SELECT parser_version FROM ops.ingestion_metadata")
                == "corrected-parser"
            )
        else:
            assert not result.ok
            assert result.missing_object_count == 1
            assert warehouse.query_value("SELECT count(*) FROM ops.heartbeat") == 0
        if repair_mode == "disabled":
            assert result.details["repair_summary"] is None
        elif repair_mode == "failure":
            assert result.failed_object_count == 1
        elif repair_mode == "stale":
            assert result.details["repair_summary"]["succeeded"] == 1
    finally:
        warehouse.close()


@pytest.mark.parametrize(
    "failure",
    [
        "fitbit",
        "dbt",
        "owntracks",
        "app-in-focus",
        "app-usage",
        "missing-relation",
        "relation-query",
        "none",
        "gap",
        "gap-covered",
        "gap-edge",
        "busy",
        "completion",
        "heartbeat",
        "old-marker-heartbeat",
    ],
)
@pytest.mark.parametrize("measure_network", [False, True])
def test_daily_records_one_outcome_and_preserves_collector_heartbeats(
    monkeypatch, capfd, failure, measure_network
):
    from personal_data_platform import dbt_runner
    from personal_data_platform import network_diagnostics as net
    from personal_data_platform.reconciliation import job
    from personal_data_platform.sources.fitbit import runtime

    monkeypatch.setenv("PDP_NETWORK_DIAGNOSTICS", str(measure_network).lower())
    monkeypatch.setattr(net.LOGGER, "handlers", [])
    monkeypatch.setattr(net.LOGGER, "level", net.LOGGER.level)
    monkeypatch.setattr(net.LOGGER, "propagate", net.LOGGER.propagate)

    logger = logging.getLogger("personal_data_platform.reconciliation")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logger, "propagate", logger.propagate)
    warehouse = _warehouse()
    successes = []
    phases = []
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "test")
    monkeypatch.setenv(
        "PDP_HEARTBEAT_CONFIG",
        '{"daily":"https://example.test/daily"}',
    )
    monkeypatch.setattr(job, "open_warehouse", lambda config=None: warehouse)
    monkeypatch.setattr(warehouse, "close", lambda: None)
    monkeypatch.setattr(job, "publish_http_heartbeat", lambda *args: successes.append(args))

    def phase(name, kwargs):
        owner = kwargs.get("_lease_owner", kwargs.get("lease_owner"))
        assert (
            warehouse.query_value("SELECT owner_id FROM ops.job_lock WHERE job_name='loader'")
            == owner
        )
        phases.append(name)
        return SimpleNamespace(ok=failure != name)

    monkeypatch.setattr(runtime, "run_daily_repair", lambda **kwargs: phase("fitbit", kwargs))

    def dbt(**kwargs):
        phase("dbt", kwargs)
        if failure == "dbt":
            raise RuntimeError("model failed")
        return 0

    monkeypatch.setattr(dbt_runner, "run_dbt_from_env", dbt)
    sources = (
        get_source("iphone_screen_time", "app-in-focus"),
        get_source("mac_screen_time", "app-usage"),
    )
    for source in sources:
        monkeypatch.setattr(type(source), "repository_from_env", lambda _: _Repository())
    owntracks = get_source("owntracks")
    monkeypatch.setattr(type(owntracks), "repository_from_env", lambda _: _Repository())

    def reconcile(repository, warehouse, **kwargs):
        result = phase("owntracks", kwargs)
        return SimpleNamespace(ok=result.ok, details={"raw_object_count": 3})

    monkeypatch.setattr(job, "run_reconciliation", reconcile)

    def audit(_warehouse, source, now):
        owner = warehouse.query_value("SELECT owner_id FROM ops.job_lock WHERE job_name='loader'")
        result = phase(source.stream, {"_lease_owner": owner})
        return SourceHealth(result.ok, {"collector_heartbeat_stale": not result.ok})

    monkeypatch.setattr(job, "audit_collector_heartbeat", audit)
    if failure == "missing-relation":
        warehouse.connection.execute("DROP VIEW marts.daily_iphone_screen_time_total")
    elif failure == "relation-query":
        warehouse.connection.execute(
            "CREATE OR REPLACE VIEW marts.daily_iphone_screen_time_total AS "
            "SELECT 1 AS value WHERE CAST('invalid' AS INTEGER) > 0"
        )
    if failure == "busy":
        warehouse.acquire_job_lock("loader", "competitor", lease_seconds=7500)
    for source in sources:
        warehouse.publish_heartbeat(source.monitor_name, "collector", {"scan_completed_at": "old"})
    collector_heartbeats = warehouse.query_rows("SELECT * FROM ops.heartbeat ORDER BY monitor_name")
    if failure == "completion":
        finish_job = warehouse.finish_job

        def broken_completion(run_id, *, succeeded, details):
            if succeeded:
                raise RuntimeError("job completion write failed")
            return finish_job(run_id, succeeded=succeeded, details=details)

        monkeypatch.setattr(warehouse, "finish_job", broken_completion)
    if failure in ("heartbeat", "old-marker-heartbeat"):

        def broken_ping(*args, **kwargs):
            raise RuntimeError("external monitor unreachable")

        monkeypatch.setattr(job, "publish_http_heartbeat", broken_ping)
    if failure.startswith("gap") or failure == "old-marker-heartbeat":

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 6, 12, tzinfo=UTC)

        monkeypatch.setattr(job, "datetime", Clock)
        warehouse.begin_job("daily-reconciliation-west", "previous")
        warehouse.finish_job(
            "previous",
            succeeded=True,
            details={
                "last_completed_target_date": {
                    "gap": "2026-09-01",
                    "gap-covered": "2026-10-03",
                    "gap-edge": "2026-10-02",
                    "old-marker-heartbeat": "2026-09-01",
                }[failure]
            },
        )
        warehouse.connection.execute(
            "UPDATE ops.job_run SET started_at=?, completed_at=? WHERE run_id='previous'",
            [Clock.now() - timedelta(days=8)] * 2,
        )
    try:
        assert job.run_reconciliation_from_env() == int(
            failure not in ("none", "gap", "gap-covered", "gap-edge", "busy")
        )
        assert len(successes) == (1 if failure in ("none", "gap", "gap-covered", "gap-edge") else 0)
        assert (
            warehouse.query_rows("SELECT * FROM ops.heartbeat ORDER BY monitor_name")
            == collector_heartbeats
        )
        outcomes = warehouse.query_rows(
            "SELECT status,details FROM ops.job_run WHERE job_name='daily-reconciliation'"
        )
        if failure == "busy":
            assert outcomes == []
        else:
            assert len(outcomes) == 1
            status, encoded = outcomes[0]
            details = json.loads(encoded)
            succeeded = failure in ("none", "gap", "gap-covered", "gap-edge")
            assert status == ("succeeded" if succeeded else "failed")
            if succeeded:
                assert "failed_stage" not in details
                assert "last_completed_target_date" in details
            else:
                expected_stage = (
                    "audit:"
                    + ("mac_screen_time" if failure == "app-usage" else "iphone_screen_time")
                    if failure.startswith("app-")
                    else failure
                )
                if failure in ("missing-relation", "relation-query"):
                    expected_stage = "audit:iphone_screen_time"
                if failure == "old-marker-heartbeat":
                    expected_stage = "heartbeat"
                assert details["failed_stage"] == expected_stage
            if failure not in ("fitbit", "dbt"):
                assert details["owntracks"] == {"raw_object_count": 3}
        assert "ops.reconciliation_run" not in _relation_names(warehouse)
        if failure in ("app-in-focus", "app-usage", "missing-relation", "relation-query"):
            entries = [json.loads(line) for line in capfd.readouterr().err.splitlines()]
            audits = [entry for entry in entries if entry.get("event") == "collector_audit"]
            assert len(audits) == 1
            assert audits[0]["severity"] == "ERROR" and audits[0]["status"] == "failed"
            summary = audits[0]["summary"]
            assert summary["stream"] == ("app-usage" if failure == "app-usage" else "app-in-focus")
            assert summary["collector_heartbeat_stale"] is failure.startswith("app-")
            assert summary["missing_relations"] == (
                ["marts.daily_iphone_screen_time_total"] if failure == "missing-relation" else []
            )
            assert summary["failed_relation_queries"] == (
                ["marts.daily_iphone_screen_time_total"] if failure == "relation-query" else None
            )
        if failure in ("none", "gap", "gap-covered", "gap-edge"):
            assert phases == ["fitbit", "dbt", "owntracks", "app-in-focus", "app-usage"]
            entries = [json.loads(line) for line in capfd.readouterr().err.splitlines()]
            assert any(
                entry.get("event") == "reconciliation"
                and entry.get("status") == "succeeded"
                and entry.get("job_name") == "daily-reconciliation"
                for entry in entries
            )
            profiles = [entry for entry in entries if entry.get("event") == "network_profile"]
            assert len(profiles) == int(measure_network)
            if measure_network:
                summary = profiles[0]["summary"]
                assert summary["workload"] == "daily"
                names = {row["phase"] for row in summary["phases"]}
                assert {
                    "daily_fitbit",
                    "daily_dbt",
                    "daily_owntracks",
                    "daily_iphone_screen_time",
                } <= names
        if failure.startswith("gap"):
            details = json.loads(
                warehouse.query_value(
                    "SELECT details FROM ops.job_run WHERE run_id!='previous' AND job_name='daily-reconciliation'"
                )
            )
            warnings = [entry for entry in entries if entry.get("event") == "fitbit_manual_repair"]
            if failure == "gap-covered":
                assert "manual_repair_required" not in details
                assert warnings == []
            else:
                expected_gap = {
                    "from": "2026-09-02" if failure == "gap" else "2026-10-03",
                    "through": "2026-10-03",
                }
                assert details["manual_repair_required"] == expected_gap
                assert len(warnings) == 1
            assert details["last_completed_target_date"] == "2026-10-05"
        if failure == "old-marker-heartbeat":
            assert warehouse.query_rows(
                "SELECT status,json_extract_string(details,'$.last_completed_target_date') "
                "FROM ops.job_run WHERE run_id='previous'"
            ) == [("succeeded", "2026-09-01")]
            monkeypatch.setattr(job, "publish_http_heartbeat", lambda *args: successes.append(args))
            assert job.run_reconciliation_from_env() == 0
            recovered = json.loads(
                warehouse.query_value(
                    "SELECT details FROM ops.job_run "
                    "WHERE job_name='daily-reconciliation' AND status='succeeded'"
                )
            )
            assert recovered["manual_repair_required"] == {
                "from": "2026-09-02",
                "through": "2026-10-03",
            }
            assert recovered["last_completed_target_date"] == "2026-10-05"
            assert (
                warehouse.query_rows("SELECT * FROM ops.heartbeat ORDER BY monitor_name")
                == collector_heartbeats
            )
        if failure == "busy":
            assert phases == []
            assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "competitor"
        else:
            assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    finally:
        warehouse.connection.close()

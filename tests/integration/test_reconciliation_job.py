"""Daily outcomes live in JSON logs and retain context on every exit path."""

import json
import logging
from types import SimpleNamespace

import pytest

from personal_data_platform.reconciliation import job
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


@pytest.mark.parametrize(
    "failure",
    [
        "logging",
        "urls",
        "policy",
        "config",
        "open",
        "migrate",
        "acquire",
        "busy",
        "fitbit",
        "fitbit-exception",
        "fitbit-dataclass",
        "dbt",
        "dbt-exception",
        "owntracks",
        "owntracks-exception",
        "missing-relation",
        "relation-query",
        "heartbeat",
        "release",
        "close",
        "none",
    ],
)
def test_daily_terminal_event_includes_all_available_outcomes(monkeypatch, capfd, failure):
    from personal_data_platform import dbt_runner
    from personal_data_platform.sources.fitbit import runtime

    logger = logging.getLogger("personal_data_platform.reconciliation")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "test")
    monkeypatch.setenv("PDP_HEARTBEAT_CONFIG", '{"daily":"https://example.test/daily"}')
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    for source in (get_source("iphone_screen_time"), get_source("mac_screen_time")):
        for relation in source.required_relations:
            if relation.startswith("marts."):
                warehouse.connection.execute(f"CREATE VIEW {relation} AS SELECT 1 AS value")
    phases, pings = [], []

    def broken(*a, **kw):
        raise RuntimeError("injected failure")

    def run_stage(name, kwargs):
        owner = kwargs.get("_lease_owner", kwargs.get("lease_owner"))
        warehouse.require_job_lock(owner)
        phases.append(name)
        if failure == name + "-exception":
            broken()
        return SimpleNamespace(ok=failure != name, details={"raw_object_count": 3})

    monkeypatch.setattr(job, "open_warehouse", lambda config=None: warehouse)
    monkeypatch.setattr(warehouse, "close", lambda: None)

    def repair(**kw):
        if failure == "fitbit-dataclass":
            from datetime import UTC, datetime, timedelta

            from personal_data_platform.sources.fitbit.acquisition import AcquisitionSummary
            from personal_data_platform.sources.fitbit.models import Window

            now = datetime.now(UTC)
            return AcquisitionSummary(
                failed_scopes=1, first_incomplete=Window("steps", now, now + timedelta(hours=1))
            )
        return run_stage("fitbit", kw)

    monkeypatch.setattr(runtime, "run_daily_repair", repair)

    def dbt(**kw):
        run_stage("dbt", kw)
        return int(failure == "dbt")

    monkeypatch.setattr(dbt_runner, "run_dbt_from_env", dbt)
    monkeypatch.setattr(job, "run_reconciliation", lambda *a, **kw: run_stage("owntracks", kw))
    monkeypatch.setattr(type(get_source("owntracks")), "repository_from_env", lambda _: object())
    monkeypatch.setattr(job, "publish_http_heartbeat", lambda *a: pings.append(a))
    if failure == "logging":
        monkeypatch.setenv("LOG_LEVEL", "bad-level")
    if failure == "close":
        monkeypatch.setattr(warehouse, "close", broken)
    if failure == "urls":
        monkeypatch.setattr(job, "daily_heartbeat_urls", broken)
    if failure == "policy":
        monkeypatch.setattr(job, "validate_runtime_policy", broken)
    if failure == "config":
        monkeypatch.delenv("MOTHERDUCK_DATABASE")
    if failure == "open":
        monkeypatch.setattr(job, "open_warehouse", broken)
    if failure == "migrate":
        monkeypatch.setattr(warehouse, "migrate", broken)
    if failure == "acquire":
        monkeypatch.setattr(warehouse, "acquire_job_lock", broken)
    if failure == "busy":
        warehouse.acquire_job_lock("loader", "competitor", lease_seconds=7500)
    if failure == "missing-relation":
        warehouse.connection.execute("DROP VIEW marts.daily_iphone_screen_time_total")
    if failure == "relation-query":
        warehouse.connection.execute(
            "CREATE OR REPLACE VIEW marts.daily_iphone_screen_time_total AS SELECT 1 WHERE CAST('bad' AS INTEGER)>0"
        )
    if failure == "heartbeat":
        monkeypatch.setattr(job, "publish_http_heartbeat", broken)
    if failure == "release":
        monkeypatch.setattr(warehouse, "release_job_lock", broken)
    try:
        result = job.run_reconciliation_from_env()
        assert result == int(failure not in {"none", "busy"})
        entries = [json.loads(line) for line in capfd.readouterr().err.splitlines()]
        events = [entry for entry in entries if entry.get("event") == "reconciliation"]
        assert [entry["status"] for entry in events] == (
            ["failed"]
            if failure == "logging"
            else [
                "running",
                "succeeded" if failure == "none" else "deferred" if failure == "busy" else "failed",
            ]
        )
        summary = events[-1]["summary"]
        assert summary["run_id"] == events[0]["summary"]["run_id"]
        assert summary["target_date"] and summary["started_at"] and summary["completed_at"]
        assert (
            summary["failed_stage"] is None
            if failure in {"none", "busy"}
            else summary["failed_stage"] is not None
        )
        if "owntracks" in phases and failure != "owntracks-exception":
            assert summary["owntracks"] == {"raw_object_count": 3}
        assert len(pings) == int(failure in {"none", "release", "close"})
        assert {r for r in warehouse.relation_names() if r.startswith("ops.")} == {
            "ops.job_lock",
            "ops.schema_migration",
        }
        if failure == "none":
            # A repeated same-day run performs its fixed repair again without a checkpoint.
            assert job.run_reconciliation_from_env() == 0
            assert phases == ["fitbit", "dbt", "owntracks"] * 2
        if failure == "busy":
            assert not phases
            assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "competitor"
    finally:
        warehouse.connection.close()


def test_relation_inventory_ignores_other_attached_databases():
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate()
        warehouse.connection.execute("ATTACH ':memory:' AS unrelated")
        warehouse.connection.execute("CREATE SCHEMA unrelated.marts")
        warehouse.connection.execute("CREATE VIEW unrelated.marts.foreign_view AS SELECT 1")
        assert "marts.foreign_view" not in warehouse.relation_names()
    finally:
        warehouse.close()

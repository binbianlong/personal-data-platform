"""Daily outcomes live in JSON logs and retain context on every exit path."""

import json
import logging
from types import SimpleNamespace

import pytest

from personal_data_platform.daily import job
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from personal_data_platform.storage.warehouse import WarehouseConnectionError


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
        "fitbit-connection",
        "fitbit-dataclass",
        "owntracks",
        "owntracks-exception",
        "missing-relation",
        "relation-query",
        "steps_heatmap",
        "heartbeat",
        "release",
        "close",
        "none",
    ],
)
def test_daily_terminal_event_includes_all_available_outcomes(monkeypatch, capfd, failure):
    from personal_data_platform.sources.fitbit import runtime

    logger = logging.getLogger("personal_data_platform.daily")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "test")
    monkeypatch.setenv("PDP_PROFILE_BUCKET", "profile")
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
        if name == "fitbit":
            warehouse.require_job_lock(kwargs["lease_owner"])
        phases.append(name)
        if failure == name + "-exception":
            broken()
        return SimpleNamespace(ok=failure != name, details={"raw_object_count": 3})

    monkeypatch.setattr(job, "open_warehouse", lambda config=None: warehouse)
    monkeypatch.setattr(warehouse, "close", lambda: None)

    def repair(**kw):
        if failure == "fitbit-connection":
            warehouse.connection_usable = False
            raise WarehouseConnectionError("injected connection failure")
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

    monkeypatch.setattr(job, "reconcile_owntracks", lambda *a, **kw: run_stage("owntracks", kw))
    monkeypatch.setattr(type(get_source("owntracks")), "repository_from_env", lambda _: object())
    monkeypatch.setattr(job, "publish_http_heartbeat", lambda *a: pings.append(a))

    def publish_steps(current_warehouse, target_date, bucket, *, timeout):
        assert current_warehouse is warehouse and bucket == "profile" and timeout > 0
        assert target_date.isoformat()
        phases.append("steps_heatmap")
        if failure == "steps_heatmap":
            broken()

    monkeypatch.setattr(job.profile_steps, "publish", publish_steps)

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
        result = job.run_daily_from_env()
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
        assert events[0]["severity"] == ("ERROR" if failure == "logging" else "INFO")
        assert events[-1]["severity"] == ("INFO" if failure in {"none", "busy"} else "ERROR")
        summary = events[-1]["summary"]
        assert "dbt" not in summary
        assert summary["run_id"] == events[0]["summary"]["run_id"]
        assert summary["target_date"] and summary["started_at"] and summary["completed_at"]
        assert (
            summary["failed_stage"] is None
            if failure in {"none", "busy"}
            else summary["failed_stage"] is not None
        )
        if "owntracks" in phases and failure != "owntracks-exception":
            assert summary["owntracks"] == {"raw_object_count": 3}
        if failure in {"fitbit", "fitbit-dataclass", "owntracks"}:
            assert summary["failed_stage"] == ("owntracks" if failure == "owntracks" else "fitbit")
            assert summary["owntracks"] == {"raw_object_count": 3}
            for source_id in ("iphone_screen_time", "mac_screen_time"):
                assert summary[source_id]["missing_relations"] == []
            assert "steps_heatmap" not in phases
        if failure in {"fitbit-exception", "fitbit-connection"}:
            assert summary["failed_stage"] == "fitbit"
            assert "owntracks" not in phases
        if failure == "steps_heatmap":
            assert summary["failed_stage"] == "steps_heatmap"
        assert len(pings) == int(failure in {"none", "release", "close"})
        assert {r for r in warehouse.relation_names() if r.startswith("ops.")} == {
            "ops.job_lock",
            "ops.schema_migration",
        }
        if failure == "none":
            # A repeated same-day run performs its fixed repair again without a checkpoint.
            assert job.run_daily_from_env() == 0
            assert phases == ["fitbit", "owntracks", "steps_heatmap"] * 2
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


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
@pytest.mark.parametrize("outcome", ["none", "busy", "fitbit"])
def test_daily_execution_history_is_independent_of_detail_log_level(
    monkeypatch, capfd, level, outcome
):
    monkeypatch.setenv("LOG_LEVEL", level)
    test_daily_terminal_event_includes_all_available_outcomes(monkeypatch, capfd, outcome)
    # Required events do not lower the configured level for ordinary messages.
    assert logging.getLogger("personal_data_platform.daily").getEffectiveLevel() == getattr(
        logging, level
    )
    logging.getLogger("personal_data_platform.daily").info("ordinary detail")
    assert ("ordinary detail" in capfd.readouterr().err) == (level in {"DEBUG", "INFO"})

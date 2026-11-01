"""Collector outcomes live in local logs, including complete empty scans."""

import json

import pytest

from personal_data_platform.biome import runtime
from personal_data_platform.sources.iphone_screen_time.adapter import IPhoneScreenTimeSource
from personal_data_platform.storage.factory import WarehouseConfig
from tests.contract.test_screen_time_collector_contract import _collector
from tests.screen_time_helpers import ScreenTimeWarehouse, event, segb


def run(collector, state, monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime, "open_warehouse", lambda config: ScreenTimeWarehouse(config.database)
    )
    return runtime.collect(
        collector,
        state,
        source=IPhoneScreenTimeSource(),
        warehouse_config=lambda: WarehouseConfig(str(tmp_path / "warehouse.duckdb")),
        poll_seconds=1,
        watch=False,
    )


def test_complete_empty_scan_succeeds_without_remote_heartbeat(tmp_path, monkeypatch, capsys):
    collector, state, _, _ = _collector(tmp_path)
    assert run(collector, state, monkeypatch, tmp_path) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["result"] == "succeeded"
    assert summary["segments"] == summary["loaded"] == 0
    assert summary["run_id"] and summary["duration_seconds"] >= 0
    with state._connect() as db:
        assert {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == {"file_state", "event_reference"}


def test_bootstrap_failure_precedes_first_scan(tmp_path, monkeypatch, capsys):
    collector, state, _, remote = _collector(tmp_path)
    (remote / "100").write_bytes(segb(event("must-not-capture"))[0])
    warehouse = ScreenTimeWarehouse(tmp_path / "warehouse.duckdb")
    warehouse.connection.execute(
        "INSERT INTO base.iphone_screen_time_event VALUES ('old', 'device', 'app', now(), 'start', true)"
    )
    warehouse.close()
    with pytest.raises(RuntimeError, match="bootstrap"):
        run(collector, state, monkeypatch, tmp_path)
    assert state.pending() == []
    summary = json.loads(capsys.readouterr().out)
    assert (summary["result"], summary["stage"]) == ("failed", "bootstrap")


def test_incomplete_scan_logs_terminal_failure_and_preserves_prior_history(
    tmp_path, monkeypatch, capsys
):
    collector, state, _, remote = _collector(tmp_path)
    (remote / "100").write_bytes(b"SEGB")
    with pytest.raises(RuntimeError, match="deferred"):
        run(collector, state, monkeypatch, tmp_path)
    summary = json.loads(capsys.readouterr().out)
    assert summary["result"] == "failed"
    assert summary["deferred"] == 1


def test_lock_contention_logs_deferred_before_capture(tmp_path, monkeypatch, capsys):
    collector, state, _, remote = _collector(tmp_path)
    (remote / "100").write_bytes(segb(event("later"))[0])
    with state.lock(), pytest.raises(RuntimeError, match="collector"):
        run(collector, state, monkeypatch, tmp_path)
    assert state.pending() == []
    summary = json.loads(capsys.readouterr().out)
    assert (summary["result"], summary["stage"]) == ("deferred", "lock")


def test_initial_bootstrap_is_serialized_with_warehouse_writers(tmp_path, monkeypatch):
    collector, state, _, _ = _collector(tmp_path)

    class LeaseCheckedWarehouse(ScreenTimeWarehouse):
        def has_screen_time_events(self, *, source_id):
            if not self.locked:
                raise RuntimeError("bootstrap read requires warehouse lease")
            return super().has_screen_time_events(source_id=source_id)

    monkeypatch.setattr(
        runtime, "open_warehouse", lambda config: LeaseCheckedWarehouse(config.database)
    )
    assert (
        runtime.collect(
            collector,
            state,
            source=IPhoneScreenTimeSource(),
            warehouse_config=lambda: WarehouseConfig(str(tmp_path / "warehouse.duckdb")),
            poll_seconds=1,
            watch=False,
        )
        == 0
    )
    with state._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)

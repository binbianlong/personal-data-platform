"""Fresh-process entrypoints emit application logs without polluting CLI results."""

import json
import os
import subprocess
import sys

import pytest

PROBE = r"""
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
import duckdb
from personal_data_platform.sources.fitbit import api, daily, runtime
from personal_data_platform.storage.motherduck import Warehouse

if sys.argv[2] == "root":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
os.environ["PDP_FITBIT_PROCESSING_PAUSED"] = "false"
os.environ["PDP_FITBIT_SUBJECT_KEY"] = "test"
runtime._repository = lambda: object()
runtime._client = lambda: object()
runtime._warehouse = lambda: Warehouse(duckdb.connect(":memory:"))
runtime.collect_daily = lambda *args, **kwargs: daily.DailySummary()
runtime.collect_range = lambda *args, **kwargs: daily.DailySummary()
command = sys.argv[1]
if command == "sync":
    now = datetime(2026, 9, 28, tzinfo=UTC)
    runtime.run_sync_from_env(start=now, end=now + timedelta(days=1), data_types=("steps",))
else:
    runtime.run_daily_from_env()
    runtime.run_daily_from_env()
api.LOGGER.info("api probe")
daily.LOGGER.info("acquisition probe")
runtime.LOGGER.error("failure probe\nsecond line")
print("ready")
"""


def _probe(command, root="default", *, level=None):
    environ = dict(os.environ)
    environ.pop("LOG_LEVEL", None)
    if level is not None:
        environ["LOG_LEVEL"] = level
    return subprocess.run(
        [sys.executable, "-c", PROBE, command, root],
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("command", ["daily", "sync"])
@pytest.mark.parametrize("root", ["default", "root"])
def test_entrypoint_enables_json_app_logs_without_duplicates(command, root):
    result = _probe(command, root)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ready\n"
    entries = [json.loads(line) for line in result.stderr.splitlines()]
    info = [entry for entry in entries if entry["severity"] == "INFO"]
    assert [entry["message"] for entry in info if "probe" in entry["message"]] == [
        "api probe",
        "acquisition probe",
    ]
    errors = [entry for entry in entries if entry["severity"] == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["message"] == "failure probe\nsecond line"
    if command == "daily":
        events = [entry for entry in entries if entry.get("event") == "fitbit_daily"]
        assert len(events) == 2
        assert all(entry["status"] == "succeeded" for entry in events)


@pytest.mark.parametrize("command", ["daily", "sync"])
def test_entrypoint_respects_log_level(command):
    result = _probe(command, level="WARNING")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ready\n"
    entries = [json.loads(line) for line in result.stderr.splitlines()]
    assert [entry["severity"] for entry in entries] == ["ERROR"]

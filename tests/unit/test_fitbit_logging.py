"""Fresh-process entrypoints emit application logs without polluting CLI results."""

import json
import os
import subprocess
import sys

import pytest

PROBE = r"""
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uvicorn
import duckdb
from personal_data_platform.sources.fitbit import api, runtime, service
from personal_data_platform.sources.fitbit.acquisition import AcquisitionSummary
from personal_data_platform.storage.motherduck import Warehouse

if sys.argv[2] == "root":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
runtime.required = lambda name: "test"
runtime.secret_config = lambda *args, **kwargs: {"authorization":"test", "health_user_id":"test"}
runtime.GoogleHealthAuthenticator = lambda **kwargs: object()
runtime.TinkSignatures = lambda: object()
runtime.create_pubsub_app = lambda **kwargs: object()
runtime.uvicorn.run = lambda app, **kwargs: uvicorn.Config(app, **kwargs)
command = sys.argv[1]
if command == "serve":
    runtime.run_serve_from_env()
elif command == "sync":
    runtime._warehouse = lambda: Warehouse(duckdb.connect())
    runtime._acquisition_runner = lambda: SimpleNamespace(
        run_windows=lambda *args, **kwargs: AcquisitionSummary(completed_scopes=1)
    )
    now = datetime(2026, 9, 28, tzinfo=UTC)
    runtime.run_sync_from_env(start=now, end=now + timedelta(days=1), data_types=("steps",))
else:
    runtime._acquisition_runner = lambda: SimpleNamespace(ingest=lambda *args,**kwargs: AcquisitionSummary())
    runtime.run_notification_job()
api.LOGGER.info("api probe")
service.LOGGER.info("acquisition probe")
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


@pytest.mark.parametrize("command", ["serve", "sync", "ingest"])
@pytest.mark.parametrize("root", ["default", "root"])
def test_entrypoint_enables_json_app_logs_without_duplicates(command, root):
    result = _probe(command, root)
    assert result.returncode == 0, result.stderr
    output = result.stdout.splitlines()
    assert output[-1] == "ready"
    if command == "sync":
        assert json.loads(output[0])["completed_scopes"] == 1
    else:
        assert len(output) == 1
    entries = [json.loads(line) for line in result.stderr.splitlines()]
    info = [entry for entry in entries if entry["severity"] == "INFO"]
    assert [entry["message"] for entry in info if "probe" in entry["message"]] == [
        "api probe",
        "acquisition probe",
    ]
    errors = [entry for entry in entries if entry["severity"] == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["message"] == "failure probe\nsecond line"


@pytest.mark.parametrize("command", ["serve", "sync", "ingest"])
def test_entrypoint_respects_log_level(command):
    result = _probe(command, level="WARNING")
    assert result.returncode == 0, result.stderr
    output = result.stdout.splitlines()
    assert output[-1] == "ready"
    if command == "sync":
        assert json.loads(output[0])["failed_scopes"] == 0
    else:
        assert len(output) == 1
    entries = [json.loads(line) for line in result.stderr.splitlines()]
    assert [entry["severity"] for entry in entries] == ["ERROR"]

import sqlite3
import sys
import types
from pathlib import Path

import pytest

from personal_data_platform import cli
from personal_data_platform.entrypoint import main


@pytest.mark.parametrize("removed_role", ["webhook", "fetch"])
def test_removed_noop_roles_are_not_accepted(removed_role: str) -> None:
    with pytest.raises(SystemExit) as error:
        main([removed_role])

    assert error.value.code == 2


def test_runtime_exception_returns_nonzero(monkeypatch, capsys) -> None:
    def fail(_args):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(cli, "_dispatch", fail)

    assert main(["loader"]) == 1
    assert "synthetic failure" in capsys.readouterr().err


def test_collection_exception_group_prints_stream_and_nested_cause(monkeypatch, capsys) -> None:
    def fail(_args):
        raise ExceptionGroup(
            "Screen Time collection failed",
            [RuntimeError("app-usage: synthetic upload failure")],
        )

    monkeypatch.setattr(cli, "_dispatch", fail)
    assert main(["screen-time", "collect", "--once"]) == 1
    assert "app-usage: synthetic upload failure" in capsys.readouterr().err


def test_devices_still_lists_iphone_when_local_mac_row_is_missing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from personal_data_platform.sources.screen_time.config import CollectorConfig

    sync_db = tmp_path / "sync.db"
    with sqlite3.connect(sync_db) as connection:
        connection.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT, me INT)"
        )
        connection.execute("INSERT INTO DevicePeer VALUES ('phone', 'Phone', 'P', 2, 0)")
    config = CollectorConfig(
        sync_db_path=sync_db,
        app_in_focus_remote_dir=tmp_path / "remote",
        state_db_path=tmp_path / "state.db",
        pseudonym_key=b"x" * 32,
        device_allowlist=frozenset(),
        mac_app_usage_local_dir=tmp_path / "local",
    )
    monkeypatch.setattr(cli.CollectorConfig, "from_env", lambda **_kwargs: config)

    assert main(["screen-time", "devices"]) == 0
    output = capsys.readouterr()
    assert '"platform": "ios"' in output.out
    assert "expected exactly one" in output.err


def test_loader_command_lazily_calls_job(monkeypatch) -> None:
    calls = []
    fake_module = types.ModuleType("personal_data_platform.loader.job")

    def run_loader_from_env(*, source_id=None, stream=None) -> int:
        assert (source_id, stream) == (None, None)
        calls.append("loader")
        return 0

    fake_module.run_loader_from_env = run_loader_from_env
    monkeypatch.setitem(sys.modules, "personal_data_platform.loader.job", fake_module)

    assert main(["loader"]) == 0
    assert calls == ["loader"]


@pytest.mark.parametrize(
    ("command", "module_name", "function_name", "extra"),
    [
        ("loader", "personal_data_platform.loader.job", "run_loader_from_env", []),
        (
            "reconciliation",
            "personal_data_platform.reconciliation.job",
            "run_reconciliation_from_env",
            [],
        ),
        (
            "rebuild",
            "personal_data_platform.recovery.rebuild",
            "run_rebuild_from_env",
            ["--dry-run"],
        ),
    ],
)
def test_all_streams_flag_reaches_runtime_job(
    monkeypatch, command, module_name, function_name, extra
) -> None:
    calls = []
    fake_module = types.ModuleType(module_name)

    def run_job(**kwargs):
        calls.append(kwargs)
        return 0

    setattr(fake_module, function_name, run_job)
    monkeypatch.setitem(sys.modules, module_name, fake_module)

    assert main([command, *extra, "--source", "screen_time", "--all-streams"]) == 0
    assert calls[0]["all_streams"] is True
    assert calls[0]["source_id"] == "screen_time"
    assert calls[0]["stream"] is None


def test_dbt_command_lazily_calls_job(monkeypatch) -> None:
    calls = []
    fake_module = types.ModuleType("personal_data_platform.dbt_runner")

    def run_dbt_from_env(*, source_id=None, stream=None) -> int:
        assert (source_id, stream) == (None, None)
        calls.append("dbt")
        return 0

    fake_module.run_dbt_from_env = run_dbt_from_env
    monkeypatch.setitem(sys.modules, "personal_data_platform.dbt_runner", fake_module)

    assert main(["dbt"]) == 0
    assert calls == ["dbt"]


@pytest.mark.parametrize(
    ("command", "module_name", "function_name"),
    [
        (
            "reconciliation",
            "personal_data_platform.reconciliation.job",
            "run_reconciliation_from_env",
        ),
        ("preflight", "personal_data_platform.preflight", "run_preflight_from_env"),
    ],
)
def test_operational_command_lazily_calls_job(
    monkeypatch,
    command: str,
    module_name: str,
    function_name: str,
) -> None:
    calls = []
    fake_module = types.ModuleType(module_name)

    def run_job(**kwargs) -> int:
        assert kwargs == (
            {"source_id": None, "stream": None} if command == "reconciliation" else {}
        )
        calls.append(command)
        return 0

    setattr(fake_module, function_name, run_job)
    monkeypatch.setitem(sys.modules, module_name, fake_module)

    assert main([command]) == 0
    assert calls == [command]


def test_rebuild_passes_selected_mode(monkeypatch) -> None:
    calls = []
    fake_module = types.ModuleType("personal_data_platform.recovery.rebuild")

    def run_rebuild_from_env(
        *,
        dry_run: bool,
        target_db: str | None,
        allow_partial_history: bool,
        source_id=None,
        stream=None,
    ) -> int:
        assert (source_id, stream) == (None, None)
        calls.append((dry_run, target_db, allow_partial_history))
        return 0

    fake_module.run_rebuild_from_env = run_rebuild_from_env
    monkeypatch.setitem(sys.modules, "personal_data_platform.recovery.rebuild", fake_module)

    assert main(["rebuild", "--dry-run"]) == 0
    assert calls == [(True, None, False)]

    assert main(["rebuild", "--target-db", "scratch", "--allow-partial-history"]) == 0
    assert calls[-1] == (False, "scratch", True)


@pytest.mark.parametrize(
    ("command", "module_name", "function_name", "mode"),
    [
        ("loader", "personal_data_platform.loader.job", "run_loader_from_env", []),
        (
            "reconciliation",
            "personal_data_platform.reconciliation.job",
            "run_reconciliation_from_env",
            [],
        ),
        (
            "rebuild",
            "personal_data_platform.recovery.rebuild",
            "run_rebuild_from_env",
            ["--dry-run"],
        ),
        ("dbt", "personal_data_platform.dbt_runner", "run_dbt_from_env", []),
    ],
)
def test_cli_passes_explicit_source_and_stream(
    monkeypatch, command, module_name, function_name, mode
):
    calls = []
    fake = types.ModuleType(module_name)

    def run_job(**kwargs):
        calls.append(kwargs)
        return 0

    setattr(fake, function_name, run_job)
    monkeypatch.setitem(sys.modules, module_name, fake)
    assert main([command, *mode, "--source", "synthetic", "--stream", "daily"]) == 0
    assert calls[0]["source_id"] == "synthetic"
    assert calls[0]["stream"] == "daily"


def test_unknown_source_is_rejected_before_cloud_access(monkeypatch, capsys):
    from personal_data_platform.sources.screen_time.adapter import ScreenTimeSource

    monkeypatch.setattr(
        ScreenTimeSource, "repository_from_env", lambda self: pytest.fail("cloud accessed")
    )
    assert main(["loader", "--source", "unknown"]) == 1
    assert "unsupported source" in capsys.readouterr().err

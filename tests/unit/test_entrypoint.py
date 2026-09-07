import sys
import types

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

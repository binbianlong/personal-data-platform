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

    def run_loader_from_env() -> int:
        calls.append("loader")
        return 0

    fake_module.run_loader_from_env = run_loader_from_env
    monkeypatch.setitem(sys.modules, "personal_data_platform.loader.job", fake_module)

    assert main(["loader"]) == 0
    assert calls == ["loader"]


def test_dbt_command_lazily_calls_job(monkeypatch) -> None:
    calls = []
    fake_module = types.ModuleType("personal_data_platform.dbt_runner")

    def run_dbt_from_env() -> int:
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

    def run_job() -> int:
        calls.append(command)
        return 0

    setattr(fake_module, function_name, run_job)
    monkeypatch.setitem(sys.modules, module_name, fake_module)

    assert main([command]) == 0
    assert calls == [command]


def test_rebuild_passes_selected_mode(monkeypatch) -> None:
    calls = []
    fake_module = types.ModuleType("personal_data_platform.recovery.rebuild")

    def run_rebuild_from_env(*, dry_run: bool, target_db: str | None) -> int:
        calls.append((dry_run, target_db))
        return 0

    fake_module.run_rebuild_from_env = run_rebuild_from_env
    monkeypatch.setitem(sys.modules, "personal_data_platform.recovery.rebuild", fake_module)

    assert main(["rebuild", "--dry-run"]) == 0
    assert calls == [(True, None)]

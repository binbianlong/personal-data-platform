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

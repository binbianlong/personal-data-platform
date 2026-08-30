import os

import pytest

from personal_data_platform import dbt_runner


def test_cloud_dbt_migrates_before_models(monkeypatch) -> None:
    calls: list[str] = []

    class FakeWarehouse:
        def __init__(self, _connection) -> None:
            calls.append("connect")

        def migrate(self) -> None:
            calls.append("migrate")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setenv("MOTHERDUCK_DATABASE", "production")
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "synthetic-token")
    monkeypatch.setattr(dbt_runner, "connect", lambda _config: object())
    monkeypatch.setattr(dbt_runner, "Warehouse", FakeWarehouse)
    monkeypatch.setattr(dbt_runner, "run_dbt", lambda *, target: calls.append(f"dbt:{target}"))

    assert dbt_runner.run_dbt_from_env() == 0
    assert calls == ["connect", "migrate", "close", "dbt:prod"]


def test_cloud_dbt_rejects_non_production_target(monkeypatch) -> None:
    monkeypatch.setenv("DBT_TARGET", "local")

    try:
        dbt_runner.run_dbt_from_env()
    except ValueError as error:
        assert str(error) == "Cloud dbt entrypoint requires DBT_TARGET=prod"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("non-production dbt target was accepted")


@pytest.mark.parametrize("previous", [None, "previous-synthetic-token"])
@pytest.mark.parametrize("fails", [False, True])
def test_dbt_masks_production_token_and_restores_environment(monkeypatch, previous, fails) -> None:
    from dbt_common.context import set_invocation_context
    from dbt_common.events.functions import env_scrubber

    secret_name = "DBT_ENV_SECRET_MOTHERDUCK_TOKEN"
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "synthetic-production-token")
    if previous is None:
        monkeypatch.delenv(secret_name, raising=False)
    else:
        monkeypatch.setenv(secret_name, previous)
    commands = []

    def invoke(arguments):
        commands.append(arguments[0])
        assert os.environ[secret_name] == "synthetic-production-token"
        set_invocation_context(os.environ)
        assert "synthetic-production-token" not in env_scrubber(
            "md:production?motherduck_token=synthetic-production-token"
        )
        if fails:
            raise RuntimeError("synthetic dbt failure")

    monkeypatch.setattr(dbt_runner, "_invoke", invoke)
    if fails:
        with pytest.raises(RuntimeError, match="synthetic dbt failure"):
            dbt_runner.run_dbt(target="prod")
    else:
        dbt_runner.run_dbt(target="prod")
    assert commands == (["run"] if fails else ["run", "test"])
    assert os.environ.get(secret_name) == previous


def test_dbt_rejects_missing_production_token_before_invocation(monkeypatch) -> None:
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setattr(dbt_runner, "_invoke", lambda _arguments: pytest.fail("dbt was invoked"))

    with pytest.raises(ValueError, match="MOTHERDUCK_TOKEN is required"):
        dbt_runner.run_dbt(target="prod")

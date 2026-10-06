import os

import pytest

from personal_data_platform import dbt_runner


def test_cloud_dbt_migrates_before_models(monkeypatch) -> None:
    import duckdb

    from personal_data_platform.storage.motherduck import Warehouse

    warehouse = Warehouse(duckdb.connect())
    calls = []
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "production")
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "synthetic-token")
    monkeypatch.setattr(dbt_runner, "connect", lambda _config: warehouse.connection)
    monkeypatch.setattr(dbt_runner, "Warehouse", lambda _: warehouse)
    monkeypatch.setattr(warehouse, "close", lambda: None)

    def run_models(**kwargs):
        assert warehouse.query_value("SELECT count(*) FROM ops.schema_migration") == 5
        assert (
            warehouse.query_value("SELECT count(*) FROM ops.job_lock WHERE expires_at > now()") == 1
        )
        calls.append(kwargs["target"])

    monkeypatch.setattr(dbt_runner, "run_dbt", run_models)
    try:
        assert dbt_runner.run_dbt_from_env() == 0
        assert calls == ["prod"]
        assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    finally:
        warehouse.connection.close()


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


def test_selected_dbt_models_and_tests_use_the_same_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(dbt_runner, "_invoke", calls.append)
    dbt_runner.run_dbt(target="local", selector="tag:screen_time_app_in_focus")
    assert [arguments[0] for arguments in calls] == ["run", "test"]
    assert all(
        arguments[-2:] == ["--select", "tag:screen_time_app_in_focus"] for arguments in calls
    )


def test_all_writers_share_loader_lease(monkeypatch):
    import duckdb

    from personal_data_platform.storage.motherduck import Warehouse

    warehouse = Warehouse(duckdb.connect(":memory:"))
    warehouse.migrate(profile="west")
    assert warehouse.acquire_job_lock("loader", "other", lease_seconds=7500)
    monkeypatch.setenv("PDP_FITBIT_DELIVERY_MODE", "pubsub")
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "production")
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "synthetic-token")
    monkeypatch.setattr(dbt_runner, "connect", lambda _: warehouse.connection)
    monkeypatch.setattr(dbt_runner, "Warehouse", lambda _: warehouse)
    monkeypatch.setattr(warehouse, "close", lambda: None)
    monkeypatch.setattr(dbt_runner, "run_dbt", lambda **kwargs: pytest.fail("dbt was invoked"))
    try:
        with pytest.raises(RuntimeError, match="lease"):
            dbt_runner.run_dbt_from_env()
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "other"
    finally:
        warehouse.connection.close()


def test_inherited_dbt_lease_is_checked_and_not_released(monkeypatch):
    import duckdb

    from personal_data_platform.storage.motherduck import Warehouse

    warehouse = Warehouse(duckdb.connect(":memory:"))
    warehouse.migrate(profile="west")
    assert warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    monkeypatch.setenv("PDP_FITBIT_DELIVERY_MODE", "pubsub")
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "production")
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "synthetic-token")
    monkeypatch.setattr(dbt_runner, "connect", lambda _: warehouse.connection)
    monkeypatch.setattr(dbt_runner, "Warehouse", lambda _: warehouse)
    monkeypatch.setattr(warehouse, "close", lambda: None)
    calls = []
    monkeypatch.setattr(dbt_runner, "run_dbt", lambda **kwargs: calls.append(kwargs))
    try:
        assert dbt_runner.run_dbt_from_env(lease_owner="daily") == 0
        assert len(calls) == 1
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "daily"
    finally:
        warehouse.connection.close()


def test_bounded_dbt_subprocess_uses_remaining_deadline(monkeypatch):
    commands = []
    monkeypatch.setattr(
        dbt_runner.subprocess, "run", lambda *args, **kwargs: commands.append((args, kwargs))
    )
    dbt_runner._invoke(["run", "--target", "local"], timeout_seconds=15)
    assert commands[0][1]["timeout"] == 15
    assert commands[0][1]["check"] is True
    with pytest.raises(TimeoutError):
        dbt_runner._invoke(["test"], timeout_seconds=0)
    assert len(commands) == 1

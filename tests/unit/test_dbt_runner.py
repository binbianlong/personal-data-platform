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

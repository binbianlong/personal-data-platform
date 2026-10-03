from personal_data_platform.cli import main


def test_daily_command_prints_summary(monkeypatch, capsys):
    import json

    from personal_data_platform.sources.fitbit import runtime
    from personal_data_platform.sources.fitbit.daily import DailySummary

    monkeypatch.setattr(
        runtime, "run_daily_from_env", lambda: DailySummary(raw_saved=1), raising=False
    )
    assert main(["fitbit", "daily"]) == 0
    assert json.loads(capsys.readouterr().out)["raw_saved"] == 1


def test_migrate_can_initialize_an_explicit_local_database(tmp_path):
    import duckdb

    database = tmp_path / "local.duckdb"
    assert main(["fitbit", "migrate", "--database", str(database)]) == 0
    with duckdb.connect(str(database)) as connection:
        assert connection.execute("select count(*) from base.fitbit_steps").fetchone() == (0,)

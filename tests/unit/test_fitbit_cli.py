from personal_data_platform.cli import main


def test_migrate_can_initialize_an_explicit_local_database(tmp_path):
    import duckdb

    database = tmp_path / "local.duckdb"
    assert main(["fitbit", "migrate", "--database", str(database)]) == 0
    with duckdb.connect(str(database)) as connection:
        assert connection.execute("select count(*) from base.fitbit_steps").fetchone() == (0,)


def test_sync_resume_id_can_reopen_persistent_range(monkeypatch):
    from personal_data_platform.sources.fitbit import runtime

    calls = []
    monkeypatch.setattr(runtime, "run_sync_from_env", lambda **kwargs: calls.append(kwargs) or 0)
    assert main(["fitbit", "sync", "--resume-id", "saved-range"]) == 0
    assert calls[0]["resume_id"] == "saved-range"
    assert calls[0]["start"] is None
    assert calls[0]["end"] is None

from personal_data_platform.cli import main


def test_migrate_can_initialize_an_explicit_local_database(tmp_path):
    import duckdb

    database = tmp_path / "local.duckdb"
    assert main(["fitbit", "migrate", "--database", str(database)]) == 0
    with duckdb.connect(str(database)) as connection:
        assert connection.execute("select count(*) from base.fitbit_steps").fetchone() == (0,)

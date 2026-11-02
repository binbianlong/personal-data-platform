from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


def test_seven_views_are_built_from_physical_base(tmp_path, monkeypatch):
    path = tmp_path / "catalog.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(path))
    run_dbt(target="local")
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    rows = warehouse.connection.execute(
        "select table_name from information_schema.views where table_schema = 'marts' order by 1"
    ).fetchall()
    assert [r[0] for r in rows] == sorted(
        [
            "iphone_screen_time_interval",
            "mac_screen_time_interval",
            "daily_steps",
            "daily_active_zone",
            "daily_iphone_screen_time_total",
            "daily_mac_screen_time_total",
            "daily_sleep_detail",
        ]
    )
    expected = {
        "iphone_screen_time_interval": "start_event_key device_key bundle_id started_at ended_at duration_seconds is_inferred boundary_event_key",
        "mac_screen_time_interval": "start_event_key device_key bundle_id started_at ended_at duration_seconds is_inferred boundary_event_key",
        "daily_steps": "activity_date steps interval_part_count has_prorated_intervals",
        "daily_active_zone": "activity_date active_zone_minutes fat_burn_azm_minutes cardio_azm_minutes peak_azm_minutes unspecified_azm_minutes interval_part_count has_prorated_intervals",
        "daily_iphone_screen_time_total": "activity_date observed_seconds inferred_seconds total_seconds unresolved_event_count",
        "daily_mac_screen_time_total": "activity_date observed_seconds inferred_seconds total_seconds unresolved_event_count",
        "daily_sleep_detail": "sleep_date kind category total_minutes",
    }
    for table, columns in expected.items():
        actual = warehouse.connection.execute(
            "select column_name from information_schema.columns where table_schema='marts' and table_name=? order by ordinal_position",
            [table],
        ).fetchall()
        assert [row[0] for row in actual] == columns.split()
        assert warehouse.connection.execute(
            "select count(*) from duckdb_columns() where schema_name='marts' and table_name=? and comment is not null",
            [table],
        ).fetchone() == (len(actual),)
    warehouse.close()


def test_screen_union_midnight_unresolved_and_correction(tmp_path, monkeypatch):
    path = tmp_path / "catalog.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    warehouse.connection.execute("""
      insert into base.iphone_screen_time_event values
      ('a','one','app','2026-01-01 14:59:59.5+00','start',true),
      ('b','one','app','2026-01-01 15:00:01.5+00','end',true),
      ('c','two','app','2026-01-01 15:00:00+00','start',true),
      ('d','two','other','2026-01-01 15:00:02+00','start',true),
      ('e','two','other','2026-01-01 15:00:03+00','end',true),
      ('f','two','app','2026-01-02 16:00:00+00','end',true)
    """)
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(path))
    run_dbt(target="local")
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    rows = warehouse.connection.execute(
        "select activity_date::varchar, observed_seconds, inferred_seconds, total_seconds, unresolved_event_count from marts.daily_iphone_screen_time_total order by 1"
    ).fetchall()
    assert rows == [
        ("2026-01-01", 0.5, 0, 0.5, 0),
        ("2026-01-02", 2.5, 0.5, 3, 0),
        ("2026-01-03", None, None, None, 1),
    ]
    warehouse.connection.execute(
        "update base.iphone_screen_time_event set is_valid=false where event_key='b'"
    )
    assert warehouse.connection.execute(
        "select total_seconds from marts.daily_iphone_screen_time_total where activity_date='2026-01-01'"
    ).fetchone() == (None,)
    warehouse.close()


def test_interval_marts_and_portable_catalog(tmp_path, monkeypatch):
    import shutil

    path = tmp_path / "stage.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    warehouse.connection.execute(
        "insert into base.steps_interval (steps_interval_id,start_at,end_at,source_date,steps) values ('s','2026-01-01 14:59:00+00','2026-01-01 15:01:00+00','2025-12-01',100)"
    )
    warehouse.connection.execute(
        "insert into base.active_zone_interval (active_zone_interval_id,start_at,end_at,active_zone_minutes,category) values ('a','2026-01-01 14:59:00+00','2026-01-01 15:01:00+00',20,'Cardio'), ('b','2026-01-01 15:00:00+00','2026-01-01 15:01:00+00',3,'mystery')"
    )
    warehouse.connection.execute(
        "insert into base.sleep_session (sleep_session_id,start_at,end_at,source_date) values ('s','2026-01-01 13:00:00+00','2026-01-01 16:00:00+00','2025-12-01')"
    )
    warehouse.connection.execute(
        "insert into base.sleep_detail (sleep_detail_id,sleep_session_id,kind,start_at,end_at,category) values ('a','s','sleep-stage','2026-01-01 13:00:00+00','2026-01-01 14:00:00+00',NULL),('b','s','sleep-stage','2026-01-01 13:30:00+00','2026-01-01 13:45:00+00',NULL)"
    )
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(path))
    run_dbt(target="local")
    canonical = tmp_path / "canonical.duckdb"
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("checkpoint")
    con.close()
    shutil.copyfile(path, canonical)
    warehouse = Warehouse(connect(WarehouseConfig(str(canonical))))
    assert warehouse.connection.execute(
        "select steps,interval_part_count,has_prorated_intervals from marts.daily_steps order by activity_date"
    ).fetchall() == [(50, 1, True), (50, 1, True)]
    assert warehouse.connection.execute(
        "select active_zone_minutes,cardio_azm_minutes,fat_burn_azm_minutes from marts.daily_active_zone order by activity_date"
    ).fetchall() == [(10, 10, None), (13, 10, None)]
    assert warehouse.connection.execute(
        "select sleep_date::varchar,kind,category,total_minutes from marts.daily_sleep_detail"
    ).fetchall() == [("2026-01-02", "sleep-stage", None, 60)]
    for table in [
        "iphone_screen_time_interval",
        "mac_screen_time_interval",
        "daily_iphone_screen_time_total",
        "daily_mac_screen_time_total",
    ]:
        warehouse.connection.execute(f"select * from marts.{table}").fetchall()
    warehouse.close()


def test_lease_loss_fails_before_view_mutation(tmp_path, monkeypatch):
    import pytest

    path = tmp_path / "catalog.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    warehouse.connection.execute(
        "insert into ops.job_lock values ('loader','owner',current_timestamp + interval '1 hour')"
    )
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(path))
    monkeypatch.setenv("PDP_DBT_LEASE_OWNER", "owner")
    run_dbt(target="local", selector="daily_steps")
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.connection.execute(
        "create or replace view marts.daily_steps as select 42 as sentinel"
    )
    warehouse.connection.execute("update ops.job_lock set owner_id='other'")
    warehouse.close()
    with pytest.raises(RuntimeError):
        run_dbt(target="local", selector="daily_steps")
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    assert warehouse.connection.execute("select * from marts.daily_steps").fetchall() == [(42,)]
    warehouse.connection.execute(
        "update ops.job_lock set owner_id='owner', expires_at=current_timestamp - interval '1 second'"
    )
    warehouse.close()
    with pytest.raises(RuntimeError):
        run_dbt(target="local", selector="daily_steps")
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    assert warehouse.connection.execute("select * from marts.daily_steps").fetchall() == [(42,)]
    warehouse.close()


def test_lease_update_rolls_back_with_model_failure(tmp_path, monkeypatch):
    import shutil

    import pytest

    from personal_data_platform.dbt_runner import DBT_PROJECT_DIR

    project = tmp_path / "project"
    shutil.copytree(DBT_PROJECT_DIR, project, ignore=shutil.ignore_patterns("target", "logs"))
    (project / "models/marts/daily_steps.sql").write_text(
        "select no_such_column from missing_table"
    )
    path = tmp_path / "fenced.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    warehouse.connection.execute(
        "insert into ops.job_lock values ('loader','owner',current_timestamp + interval '1 hour')"
    )
    before = warehouse.connection.execute("select expires_at from ops.job_lock").fetchone()
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(path))
    monkeypatch.setenv("PDP_DBT_LEASE_OWNER", "owner")
    with pytest.raises(RuntimeError):
        run_dbt(target="local", project_dir=project, selector="daily_steps")
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    assert warehouse.connection.execute("select expires_at from ops.job_lock").fetchone() == before
    warehouse.close()


def test_source_selectors_work_without_unrelated_tables(tmp_path, monkeypatch):

    for selector in ("iphone_screen_time", "mac_screen_time", "fitbit", "owntracks"):
        path = tmp_path / f"{selector}.duckdb"
        warehouse = Warehouse(connect(WarehouseConfig(str(path))))
        warehouse.migrate()
        keep = {
            "iphone_screen_time": ["iphone_screen_time_event"],
            "mac_screen_time": ["mac_screen_time_event"],
            "fitbit": ["steps_interval", "active_zone_interval", "sleep_session", "sleep_detail"],
            "owntracks": ["location_point"],
        }[selector]
        tables = warehouse.connection.execute(
            "select table_name from information_schema.tables where table_schema='base'"
        ).fetchall()
        for (table,) in tables:
            if table not in keep:
                warehouse.connection.execute(f'drop table base."{table}"')
        warehouse.close()
        monkeypatch.setenv("DBT_DUCKDB_PATH", str(path))
        run_dbt(target="local", selector=f"tag:{selector}")


def test_tied_boundaries_and_os_totals_are_independent(tmp_path, monkeypatch):
    path = tmp_path / "events.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    for table in ("iphone_screen_time_event", "mac_screen_time_event"):
        warehouse.connection.execute(f"""
          insert into base.{table} values
          ('a','one','app','2026-01-01 00:00:00+00','start',true),
          ('b','one','app','2026-01-01 00:00:10+00','end',true),
          ('c','one','other','2026-01-01 00:00:10+00','start',true),
          ('d','one','other','2026-01-01 00:00:15+00','end',true),
          ('e','one','zero','2026-01-02 00:00:00+00','start',true),
          ('f','one','zero','2026-01-02 00:00:00+00','end',true)
        """)
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(path))
    run_dbt(target="local")
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    for name in ("iphone", "mac"):
        assert warehouse.connection.execute(
            f"select start_event_key,boundary_event_key,is_inferred from marts.{name}_screen_time_interval order by started_at"
        ).fetchall() == [("a", "b", False), ("c", "d", False)]
        assert warehouse.connection.execute(
            f"select observed_seconds,total_seconds,unresolved_event_count from marts.daily_{name}_screen_time_total order by activity_date"
        ).fetchall() == [(15, 15, 0), (None, None, 2)]
    warehouse.close()


def test_prod_source_only_and_model_fencing_with_local_profile(tmp_path, monkeypatch):
    import shutil

    import pytest

    from personal_data_platform.dbt_runner import DBT_PROJECT_DIR

    project = tmp_path / "project"
    shutil.copytree(DBT_PROJECT_DIR, project, ignore=shutil.ignore_patterns("target", "logs"))
    (project / "profiles.yml").write_text("""personal_data_platform:
  target: prod
  outputs:
    prod:
      type: duckdb
      path: "{{ env_var('DBT_DUCKDB_PATH') }}"
      schema: main
      threads: 1
""")
    path = tmp_path / "catalog.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    warehouse.migrate()
    warehouse.connection.execute("create view marts.daily_steps as select 42 as sentinel")
    warehouse.connection.execute(
        "insert into ops.job_lock values ('loader','owner',current_timestamp + interval '1 hour')"
    )
    before = warehouse.connection.execute("select expires_at from ops.job_lock").fetchone()[0]
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(path))
    monkeypatch.delenv("PDP_DBT_LEASE_OWNER", raising=False)
    config = WarehouseConfig("dummy", "dummy")
    run_dbt(target="prod", project_dir=project, selector="tag:owntracks", config=config)
    with pytest.raises(RuntimeError):
        run_dbt(target="prod", project_dir=project, selector="daily_steps", config=config)
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    assert warehouse.connection.execute("select * from marts.daily_steps").fetchall() == [(42,)]
    assert (
        warehouse.connection.execute("select expires_at from ops.job_lock").fetchone()[0] == before
    )
    warehouse.close()
    monkeypatch.setenv("PDP_DBT_LEASE_OWNER", "owner")
    run_dbt(target="prod", project_dir=project, selector="daily_steps", config=config)
    warehouse = Warehouse(connect(WarehouseConfig(str(path))))
    assert (
        warehouse.connection.execute("select expires_at from ops.job_lock").fetchone()[0] < before
    )
    assert warehouse.connection.execute("select * from marts.daily_steps").fetchall() == []
    warehouse.close()


def test_owntracks_rebuild_runs_source_tests_with_prod_profile(tmp_path, monkeypatch):
    import shutil
    from types import SimpleNamespace

    from personal_data_platform.dbt_runner import DBT_PROJECT_DIR
    from personal_data_platform.recovery import rebuild
    from personal_data_platform.sources.registry import get_source

    project = tmp_path / "project"
    shutil.copytree(DBT_PROJECT_DIR, project, ignore=shutil.ignore_patterns("target", "logs"))
    (project / "profiles.yml").write_text("""personal_data_platform:
  target: prod
  outputs:
    prod:
      type: duckdb
      path: "{{ env_var('DBT_DUCKDB_PATH') }}"
      schema: main
      threads: 1
""")
    path = tmp_path / "scratch.duckdb"
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(path))
    monkeypatch.delenv("PDP_DBT_LEASE_OWNER", raising=False)
    monkeypatch.setattr(
        rebuild, "open_warehouse", lambda config: Warehouse(connect(WarehouseConfig(str(path))))
    )
    monkeypatch.setattr(rebuild, "run_dbt", lambda **kwargs: run_dbt(project_dir=project, **kwargs))
    assert (
        rebuild.run_rebuild(
            SimpleNamespace(list_raw=lambda prefix: []),
            target_db="scratch",
            token="dummy",
            production_db="production",
            allow_partial_history=True,
            source=get_source("owntracks"),
        )
        == 0
    )

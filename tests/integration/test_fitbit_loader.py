from datetime import UTC, datetime, timedelta

import duckdb

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.storage.motherduck import Warehouse
from tests.integration.test_dbt_models import dbt_project  # noqa: F401


def test_targeted_loader_never_lists_or_migrates():
    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.models import Snapshot, Window
    from personal_data_platform.sources.fitbit.raw import encode_snapshot

    now = datetime(2026, 9, 1, tzinfo=UTC)
    data = Snapshot("self", Window("steps", now, now + timedelta(days=1)), now, ())
    key, compressed = encode_snapshot(data)
    source = FitbitSource()
    raw = source.parse_raw_key(key, storage_created_at=now, storage_generation=321)
    calls = []

    class Repository:
        def list_raw(self, *args):
            raise AssertionError("targeted loader must not list")

        def get_raw(self, key, *, generation):
            calls.append(generation)
            return compressed

    warehouse = Warehouse(duckdb.connect())
    warehouse.migrate()
    warehouse.migrate = lambda: (_ for _ in ()).throw(AssertionError("no migrate"))
    try:
        assert run_loader_objects(Repository(), warehouse, [raw], source=source).succeeded == 1
        assert run_loader_objects(Repository(), warehouse, [raw], source=source).skipped == 1
        assert calls == [321]
    finally:
        warehouse.close()


def test_fitbit_reconciliation_checks_schema_and_expires_old_raw(
    tmp_path,
    monkeypatch,
    dbt_project,  # noqa: F811
):
    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.reconciliation.job import run_reconciliation
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.models import Snapshot, Window
    from personal_data_platform.sources.fitbit.raw import encode_snapshot

    now = datetime(2026, 9, 27, tzinfo=UTC)
    old = now - timedelta(days=94)
    source = FitbitSource()
    key, compressed = encode_snapshot(
        Snapshot("self", Window("steps", old, old + timedelta(days=1)), old, ())
    )
    raw = source.parse_raw_key(key, storage_created_at=old, storage_generation=123)

    class Repository:
        def list_raw(self, prefix):
            return []

        def get_raw(self, key, *, generation):
            return compressed

    database = str(tmp_path / "audit.duckdb")
    warehouse = Warehouse(duckdb.connect(database))
    warehouse.migrate()
    assert run_loader_objects(Repository(), warehouse, [raw], source=source).ok
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", database)
    run_dbt(target="local", project_dir=dbt_project, selector=source.dbt_selector)
    warehouse = Warehouse(duckdb.connect(database))
    try:
        result = run_reconciliation(
            Repository(), warehouse, source=source, heartbeat=lambda _: None, now=now
        )
        assert result.ok, result.details
        assert result.details["newly_expired_object_count"] == 1
        warehouse.connection.execute("DROP VIEW marts.daily_fitbit_health")
        result = run_reconciliation(
            Repository(), warehouse, source=source, heartbeat=lambda _: None, now=now
        )
        assert not result.ok
        assert "marts.daily_fitbit_health" in result.missing_relations
    finally:
        warehouse.close()

from datetime import UTC, datetime, timedelta

import duckdb

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.storage.motherduck import Warehouse


def test_targeted_loader_never_lists_or_migrates():
    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.models import Snapshot, Window
    from tests.fitbit_helpers import encode_snapshot

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
    dbt_project,
):
    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.reconciliation.job import run_reconciliation
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.models import Snapshot, Window
    from tests.fitbit_helpers import encode_snapshot

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


def test_buffered_load_has_no_storage_read():
    import gzip
    from datetime import UTC, datetime, timedelta

    import duckdb

    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.models import (
        CapturedSnapshot,
        FitbitBundle,
        Snapshot,
        Window,
    )
    from personal_data_platform.sources.fitbit.raw import encode_bundle
    from personal_data_platform.storage.motherduck import Warehouse

    when = datetime(2026, 10, 1, tzinfo=UTC)
    data = CapturedSnapshot(
        Snapshot("self", Window("steps", when, when + timedelta(days=1)), when, ()),
        ({"unknown": 1},),
    )
    chunks = encode_bundle(FitbitBundle("bundle", (data,)))
    source = FitbitSource(version=3)
    refs = tuple(
        source.parse_raw_key(key, storage_created_at=when, storage_generation=1)
        for key, _ in chunks
    )

    class NoReads:
        def get_raw(self, *args, **kwargs):
            raise AssertionError("buffered load must not GET")

        def list_raw(self, *args, **kwargs):
            raise AssertionError("buffered load must not LIST")

    warehouse = Warehouse(duckdb.connect())
    warehouse.migrate()
    try:
        summary = run_loader_objects(
            NoReads(), warehouse, refs, source=source, buffered_payloads=dict(chunks)
        )
        assert summary.ok and summary.succeeded == len(chunks)
        assert warehouse.query_value(
            "SELECT count(*) FROM ops.ingestion_metadata WHERE status='succeeded'"
        ) == len(chunks)
        assert (
            warehouse.query_value("SELECT source_sha256 FROM ops.fitbit_coverage")
            == data.source_sha256()
        )
        payload = gzip.decompress(chunks[0][1])
        assert refs[0].sha256 == __import__("hashlib").sha256(payload).hexdigest()
    finally:
        warehouse.close()


def test_raw_object_failure_preserves_all_acquisitions(monkeypatch):
    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.models import (
        CapturedSnapshot,
        FitbitBundle,
        Record,
        Snapshot,
        Window,
    )
    from personal_data_platform.sources.fitbit.raw import encode_bundle
    from personal_data_platform.sources.fitbit.writer import FitbitBatch

    when = datetime(2026, 10, 1, tzinfo=UTC)
    first = Window("steps", when, when + timedelta(days=1))
    second = Window("steps", when + timedelta(days=1), when + timedelta(days=2))
    warehouse = Warehouse(duckdb.connect())
    warehouse.migrate(profile="west")
    old = Snapshot(
        "self",
        first,
        when - timedelta(minutes=1),
        (Record("steps", "old", when, when, when + timedelta(minutes=1), 10.0),),
    )
    FitbitBatch(old).write_snapshot(warehouse.connection, source_key="old", loaded_at=when)
    entries = tuple(
        CapturedSnapshot(Snapshot("self", window, when + timedelta(seconds=1), ()), ())
        for window in (first, second)
    )
    objects = encode_bundle(FitbitBundle("atomic", entries))
    assert len(objects) == 1
    source = FitbitSource(version=3)
    reference = source.parse_raw_key(objects[0][0], storage_created_at=when, storage_generation=1)
    original = FitbitBatch.write_snapshot

    def interrupted(self, *args, **kwargs):
        if self.snapshot.window == second:
            raise RuntimeError("second acquisition interrupted")
        return original(self, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(FitbitBatch, "write_snapshot", interrupted)
            result = run_loader_objects(
                object(), warehouse, (reference,), source=source, buffered_payloads=dict(objects)
            )
        assert not result.ok
        assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") == 10.0
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM ops.ingestion_metadata WHERE status='succeeded'"
            )
            == 0
        )
        assert run_loader_objects(
            object(), warehouse, (reference,), source=source, buffered_payloads=dict(objects)
        ).ok
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 0
    finally:
        warehouse.close()

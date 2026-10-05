from datetime import UTC, datetime, timedelta

import duckdb
import pytest

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


def test_buffered_load_has_no_storage_read():
    import gzip
    from datetime import UTC, datetime, timedelta

    import duckdb

    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.models import (
        BundleEntry,
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
    chunks = encode_bundle(FitbitBundle("bundle", (BundleEntry("attempt", data),)))
    source = FitbitSource(version=2)
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
            warehouse.query_value(
                "SELECT status FROM ops.fitbit_attempt WHERE attempt_id='attempt'"
            )
            == "succeeded"
        )
        payload = gzip.decompress(chunks[0][1])
        assert refs[0].sha256 == __import__("hashlib").sha256(payload).hexdigest()
    finally:
        warehouse.close()


@pytest.mark.parametrize("failure", ["missing_chunk", "last_chunk_write"])
def test_bundle_failure_preserves_data_and_pending_attempt(monkeypatch, failure):
    import hashlib
    import random

    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.sources.fitbit import raw
    from personal_data_platform.sources.fitbit.acquisition_state import AcquisitionState
    from personal_data_platform.sources.fitbit.adapter import FitbitSource
    from personal_data_platform.sources.fitbit.models import (
        BundleEntry,
        CapturedSnapshot,
        FitbitBundle,
        Notification,
        Record,
        Snapshot,
        Window,
    )
    from personal_data_platform.sources.fitbit.writer import FitbitBatch

    when = datetime(2026, 10, 1, tzinfo=UTC)
    window = Window("steps", when, when + timedelta(days=1))
    warehouse = Warehouse(duckdb.connect())
    warehouse.migrate()
    state = AcquisitionState(warehouse)
    scope = state.register_notifications((Notification("n", "self", (window,), when),))["n"][0]
    attempt = state.start_attempt(scope, started_at=when + timedelta(seconds=1))
    state.bind_attempt(("n",), scope, attempt)
    old = Snapshot(
        "self",
        window,
        when - timedelta(minutes=1),
        (Record("steps", "old", when, when, when + timedelta(minutes=1), 10.0),),
    )
    warehouse.connection.execute("BEGIN")
    FitbitBatch(old).write_snapshot(warehouse.connection, source_key="old", loaded_at=when)
    warehouse.connection.execute("COMMIT")
    acquisition = CapturedSnapshot(
        Snapshot("self", window, when + timedelta(seconds=1), ()),
        ({"unknown": random.Random(1).randbytes(5000).hex()},),
    )
    monkeypatch.setattr(raw, "MAX_COMPRESSED_BYTES", 2048)
    chunks = raw.encode_bundle(FitbitBundle("atomic", (BundleEntry(attempt, acquisition),)))
    assert len(chunks) > 1
    source = FitbitSource(version=2)
    refs = tuple(
        source.parse_raw_key(key, storage_created_at=when, storage_generation=1)
        for key, _ in chunks
    )
    state.prepare_bundle(
        "atomic",
        (attempt,),
        tuple((key, hashlib.sha256(data).hexdigest(), len(data)) for key, data in chunks),
    )
    for reference in refs:
        state.mark_chunk_saved(reference.key, reference.storage_generation)
    original = warehouse.load_object

    def interrupted(reference, **kwargs):
        if reference.key == refs[-1].key:
            raise RuntimeError("last chunk interrupted")
        return original(reference, **kwargs)

    try:
        with monkeypatch.context() as patch:
            if failure == "last_chunk_write":
                patch.setattr(warehouse, "load_object", interrupted)
            result = run_loader_objects(
                object(),
                warehouse,
                refs[:-1] if failure == "missing_chunk" else refs,
                source=source,
                buffered_payloads=dict(chunks),
            )
        assert not result.ok
        assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") == 10.0
        assert state.ackable_ids(("n",)) == frozenset()
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM ops.ingestion_metadata WHERE status='succeeded'"
            )
            == 0
        )
        assert run_loader_objects(
            object(), warehouse, refs, source=source, buffered_payloads=dict(chunks)
        ).ok
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 0
        assert state.ackable_ids(("n",)) == frozenset({"n"})
    finally:
        warehouse.close()

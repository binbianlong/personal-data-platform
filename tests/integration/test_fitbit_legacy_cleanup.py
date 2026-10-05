from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import _raw, _record


class Bucket:
    name = "old-raw"

    def __init__(self):
        self.objects = {
            "raw/fitbit/v1/subject/steps/a.json": 10,
            "raw/fitbit/v1/control/device-sync.json": 11,
            "receipts/fitbit/v1/receipt.json": 12,
            "raw/fitbit/v2/bundle.json": 20,
            "raw/screen_time/v1/segment.bin": 30,
        }

    def list_blobs(self, *, prefix):
        return [
            SimpleNamespace(name=name, generation=generation)
            for name, generation in self.objects.items()
            if name.startswith(prefix)
        ]

    def blob(self, name):
        bucket = self

        class Blob:
            def reload(self):
                if name not in bucket.objects:
                    raise NotFound("missing")
                self.generation = bucket.objects[name]

            def delete(self, *, if_generation_match):
                if name not in bucket.objects:
                    raise NotFound("missing")
                if bucket.objects[name] != if_generation_match:
                    raise PreconditionFailed("generation mismatch")
                del bucket.objects[name]

        return Blob()


class Tasks:
    queue = "projects/p/locations/us-central1/queues/old-fitbit"

    def __init__(self):
        self.names = {
            self.queue + "/tasks/old-1",
            self.queue + "/tasks/old-2",
            "projects/p/locations/us-west1/queues/new/tasks/new-1",
        }
        self.state = 2  # Cloud Tasks Queue.State.PAUSED

    def list_tasks(self, *, request):
        return [
            SimpleNamespace(name=name)
            for name in self.names
            if name.startswith(request["parent"] + "/tasks/")
        ]

    def get_queue(self, *, request):
        assert request["name"] == self.queue
        return SimpleNamespace(state=self.state)

    def delete_task(self, *, request):
        if request["name"] not in self.names:
            raise NotFound("missing")
        self.names.remove(request["name"])


@pytest.fixture
def fixture():
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    raw = _raw()
    warehouse.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
    warehouse.connection.execute(
        "INSERT INTO ops.fitbit_raw_intent VALUES "
        "('receipt',0,'s','steps',now(),now()+INTERVAL '1 day', 'raw/fitbit/v1/a',now())"
    )
    warehouse.connection.execute("INSERT INTO ops.fitbit_notification VALUES ('new','s',now())")
    warehouse.connection.execute(
        "INSERT INTO base.fitbit_steps (subject_key,record_id,cursor_at,start_at,origin,"
        "fetched_at,source_key,loaded_at) VALUES ('s','old',now(),now(),'api',now(),'raw/fitbit/v1/a',now())"
    )
    warehouse.connection.execute(
        "INSERT INTO ops.fitbit_coverage VALUES ('s','steps',now(),now()+INTERVAL '1 day',"
        "now(),'api','raw/fitbit/v1/a','digest','source-digest')"
    )
    warehouse.connection.execute(
        "INSERT INTO base.fitbit_heart_rate_minute VALUES ('s','google-wearables',"
        "'2026-10-01 00:00:00+00','minute-v1','2026-10-01 00:01:00+00',60,55,65,NULL,"
        "'api',now(),'raw/fitbit/v2/a',now())"
    )
    warehouse.connection.execute(
        "INSERT INTO ops.job_lock VALUES ('loader','new-writer',now()+INTERVAL '1 hour')"
    )
    warehouse.connection.execute(
        "INSERT INTO ops.ingestion_metadata "
        "(object_key,source_id,schema_version,subject_key,source_stream,logical_key,observed_at,"
        "content_sha256,byte_size,status,started_at) VALUES "
        "('raw/fitbit/v1/a','fitbit',1,'s','steps','old',now(),'hash',1,'succeeded',now()),"
        "('raw/fitbit/v2/a','fitbit',2,'s','bundle','new',now(),'hash',1,'succeeded',now())"
    )
    yield warehouse, Bucket(), Tasks()
    warehouse.close()


def resources():
    from scripts.cleanup_fitbit_legacy import ResourceIds

    return ResourceIds("memory", "old-raw", Tasks.queue)


def new_resources():
    from scripts.cleanup_fitbit_legacy import ResourceIds

    return ResourceIds("west", "new-raw", "projects/p/locations/us-west1/queues/new")


def inventory(fixture):
    from scripts.cleanup_fitbit_legacy import inventory_legacy

    warehouse, bucket, tasks = fixture
    return inventory_legacy(
        warehouse.connection, bucket, tasks, old=resources(), new=new_resources()
    )


def apply(fixture, manifest, **kwargs):
    from scripts.cleanup_fitbit_legacy import apply_manifest, manifest_sha256

    warehouse, bucket, tasks = fixture
    return apply_manifest(
        warehouse.connection,
        bucket,
        tasks,
        manifest,
        old=resources(),
        new=new_resources(),
        writers_stopped=True,
        reviewed_sha256=manifest_sha256(manifest),
        **kwargs,
    )


def test_legacy_cleanup_preserves_screen_time_and_schema_migrations(fixture):
    warehouse, bucket, tasks = fixture
    protected = (
        "ops.schema_migration",
        "base.screen_time_event",
        "ops.screen_time_record",
        "ops.job_lock",
        "ops.fitbit_notification",
        "base.fitbit_heart_rate_minute",
    )
    before = {table: warehouse.query_rows(f"SELECT * FROM {table}") for table in protected}
    manifest = inventory(fixture)
    assert len(manifest["objects"]) == 3
    assert len(manifest["tasks"]) == 2
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 1
    assert len(bucket.objects) == 5  # Inventory is strictly read-only.
    apply(fixture, manifest)
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 0
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 0
    assert warehouse.query_rows(
        "SELECT object_key FROM ops.ingestion_metadata WHERE source_id='fitbit'"
    ) == [("raw/fitbit/v2/a",)]
    assert {table: warehouse.query_rows(f"SELECT * FROM {table}") for table in protected} == before
    assert bucket.objects == {"raw/fitbit/v2/bundle.json": 20, "raw/screen_time/v1/segment.bin": 30}
    assert tasks.names == {"projects/p/locations/us-west1/queues/new/tasks/new-1"}
    apply(fixture, manifest)
    assert {table: warehouse.query_rows(f"SELECT * FROM {table}") for table in protected} == before


@pytest.mark.parametrize("field", ["database", "bucket", "queue"])
def test_legacy_cleanup_rejects_new_resources(fixture, field):
    from scripts.cleanup_fitbit_legacy import ResourceIds, inventory_legacy

    old = dict(database="memory", bucket="old-raw", queue=Tasks.queue)
    old[field] = getattr(new_resources(), field)
    warehouse, bucket, tasks = fixture
    with pytest.raises(ValueError, match="new resource"):
        inventory_legacy(
            warehouse.connection, bucket, tasks, old=ResourceIds(**old), new=new_resources()
        )
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 1


@pytest.mark.parametrize("bad_scope", ["table", "object", "task"])
def test_cleanup_rejects_unallowlisted_manifest_targets_before_mutation(fixture, bad_scope):
    manifest = inventory(fixture)
    if bad_scope == "table":
        manifest["tables"][0]["table"] = "ops.schema_migration"
    elif bad_scope == "object":
        manifest["objects"][0]["key"] = "raw/fitbit/v2/bundle.json"
    else:
        manifest["tasks"][0] = "projects/p/locations/us-west1/queues/new/tasks/new-1"
    with pytest.raises(ValueError, match="allowlist"):
        apply(fixture, manifest)
    assert fixture[0].query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 1
    assert len(fixture[1].objects) == 5


def test_cleanup_rejects_changed_generation_before_database_mutation(fixture):
    manifest = inventory(fixture)
    fixture[1].objects["raw/fitbit/v1/subject/steps/a.json"] = 99
    with pytest.raises(ValueError, match="generation"):
        apply(fixture, manifest)
    assert fixture[0].query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 1
    assert len(fixture[2].names) == 3


def test_cleanup_rejects_modified_rows_and_preserves_unreviewed_rows(fixture):
    manifest = inventory(fixture)
    warehouse = fixture[0]
    warehouse.connection.execute("UPDATE ops.fitbit_raw_intent SET subject_key='changed'")
    with pytest.raises(ValueError, match="row changed"):
        apply(fixture, manifest)
    warehouse.connection.execute("UPDATE ops.fitbit_raw_intent SET subject_key='s'")
    warehouse.connection.execute(
        "INSERT INTO ops.fitbit_raw_intent SELECT receipt_key,work_index+1,subject_key,data_type,"
        "range_start,range_end,raw_key||'-unreviewed',fetched_at FROM ops.fitbit_raw_intent"
    )
    apply(fixture, manifest)
    assert warehouse.query_rows("SELECT raw_key FROM ops.fitbit_raw_intent") == [
        ("raw/fitbit/v1/a-unreviewed",)
    ]


def test_cleanup_requires_stopped_writers_paused_queue_and_reviewed_digest(fixture):
    from scripts.cleanup_fitbit_legacy import apply_manifest, manifest_sha256

    manifest = inventory(fixture)
    warehouse, bucket, tasks = fixture
    args = dict(
        old=resources(),
        new=new_resources(),
        writers_stopped=False,
        reviewed_sha256=manifest_sha256(manifest),
    )
    with pytest.raises(ValueError, match="writers"):
        apply_manifest(warehouse.connection, bucket, tasks, manifest, **args)
    args["writers_stopped"] = True
    tasks.state = 1
    with pytest.raises(ValueError, match="paused"):
        apply_manifest(warehouse.connection, bucket, tasks, manifest, **args)
    tasks.state = 2
    changed = copy.deepcopy(manifest)
    changed["tasks"] = []
    with pytest.raises(ValueError, match="reviewed"):
        apply_manifest(warehouse.connection, bucket, tasks, changed, **args)
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 1


def test_cleanup_protects_new_pubsub_subscription_id(fixture):
    from scripts.cleanup_fitbit_legacy import (
        ResourceIds,
        apply_manifest,
        inventory_legacy,
        manifest_sha256,
    )

    warehouse, bucket, tasks = fixture
    protected = ResourceIds("west", "new-raw", "projects/p/subscriptions/new-fitbit")
    manifest = inventory_legacy(warehouse.connection, bucket, tasks, old=resources(), new=protected)
    assert manifest["new"]["queue"] == "projects/p/subscriptions/new-fitbit"
    apply_manifest(
        warehouse.connection,
        bucket,
        tasks,
        manifest,
        old=resources(),
        new=protected,
        writers_stopped=True,
        reviewed_sha256=manifest_sha256(manifest),
    )
    assert warehouse.query_rows("SELECT notification_id FROM ops.fitbit_notification") == [("new",)]


@pytest.mark.parametrize("field", ["database", "bucket"])
def test_cleanup_rejects_connected_resource_identity_mismatch(fixture, field):
    from scripts.cleanup_fitbit_legacy import ResourceIds, inventory_legacy

    ids = dict(database="memory", bucket="old-raw", queue=Tasks.queue)
    ids[field] = "unrelated"
    warehouse, bucket, tasks = fixture
    with pytest.raises(ValueError, match="does not match"):
        inventory_legacy(
            warehouse.connection, bucket, tasks, old=ResourceIds(**ids), new=new_resources()
        )
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_raw_intent") == 1


def test_cleanup_preserves_v2_metric_and_coverage_rows_in_shared_legacy_tables(fixture):
    warehouse = fixture[0]
    warehouse.connection.execute(
        "INSERT INTO base.fitbit_steps SELECT subject_key,'new',cursor_at,start_at,end_at,value,"
        "offset_seconds,end_offset_seconds,source_date,parent_id,category,is_main_sleep,origin,"
        "fetched_at,'raw/fitbit/v2/a',loaded_at FROM base.fitbit_steps"
    )
    warehouse.connection.execute(
        "INSERT INTO ops.fitbit_coverage SELECT subject_key,data_type,range_start+INTERVAL '1 day',"
        "range_end+INTERVAL '1 day',fetched_at,origin,'raw/fitbit/v2/a',content_sha256,source_sha256 "
        "FROM ops.fitbit_coverage"
    )
    apply(fixture, inventory(fixture))
    assert warehouse.query_rows("SELECT record_id FROM base.fitbit_steps") == [("new",)]
    assert warehouse.query_rows("SELECT source_key FROM ops.fitbit_coverage") == [
        ("raw/fitbit/v2/a",)
    ]

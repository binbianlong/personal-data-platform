"""Inventory and remove reviewed legacy Fitbit rows, v1 objects and Cloud Tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.api_core.exceptions import NotFound

from personal_data_platform.storage.motherduck import WarehouseConfig, connect

PREFIXES = ("raw/fitbit/v1/", "receipts/fitbit/v1/")
# Final minute/acquisition tables, migration receipts, Screen Time and leases are excluded.
TABLE_SCOPES = {
    **{
        f"base.fitbit_{name}": "starts_with(source_key, 'raw/fitbit/v1/')"
        for name in (
            "steps",
            "heart_rate",
            "resting_heart_rate",
            "active_zone",
            "sleep",
            "sleep_stage",
            "sleep_wake",
        )
    },
    "ops.fitbit_coverage": "starts_with(source_key, 'raw/fitbit/v1/')",
    "ops.fitbit_deleted_record": "starts_with(source_key, 'raw/fitbit/v1/')",
    "ops.fitbit_raw_intent": "starts_with(raw_key, 'raw/fitbit/v1/')",
    "ops.ingestion_metadata": (
        "source_id = 'fitbit' AND (starts_with(object_key, 'raw/fitbit/v1/') "
        "OR starts_with(object_key, 'receipts/fitbit/v1/'))"
    ),
}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_QUEUE = re.compile(r"^projects/[^/]+/locations/[^/]+/queues/[^/]+$")
_PUBSUB = re.compile(r"^projects/[^/]+/(?:topics|subscriptions)/[^/]+$")


@dataclass(frozen=True)
class ResourceIds:
    database: str
    bucket: str
    queue: str


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    return value


def manifest_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _resources(connection, bucket, old: ResourceIds, new: ResourceIds) -> None:
    for name in ("database", "bucket", "queue"):
        if not getattr(old, name) or not getattr(new, name):
            raise ValueError(f"both old and new {name} IDs are required")
        if getattr(old, name) == getattr(new, name):
            raise ValueError(f"cleanup refuses a new resource: {name}")
    if not _QUEUE.fullmatch(old.queue):
        raise ValueError("old cleanup queue must be a Cloud Tasks queue resource ID")
    if not (_QUEUE.fullmatch(new.queue) or _PUBSUB.fullmatch(new.queue)):
        raise ValueError("protected new queue must be a Cloud Tasks or Pub/Sub resource ID")
    if bucket.name != old.bucket:
        raise ValueError("connected bucket does not match the old resource ID")
    if connection.execute("SELECT current_database()").fetchone()[0] != old.database:
        raise ValueError("connected database does not match the old resource ID")


def _table_inventory(connection, table: str) -> dict:
    columns = [row[0] for row in connection.execute(f"DESCRIBE {table}").fetchall()]
    if not columns or any(not _IDENTIFIER.fullmatch(name) for name in columns):
        raise ValueError("unexpected cleanup columns")
    expression = "hash(" + ",".join(f'"{name}"' for name in columns) + ")"
    count, total, xor = connection.execute(
        f"SELECT count(*), coalesce(sum({expression}::HUGEINT),0)::VARCHAR, "
        f"coalesce(bit_xor({expression}),0)::VARCHAR FROM {table} WHERE {TABLE_SCOPES[table]}"
    ).fetchone()
    return {"table": table, "columns": columns, "row_count": count, "fingerprint": [total, xor]}


def inventory_legacy(connection, bucket, tasks, *, old: ResourceIds, new: ResourceIds) -> dict:
    """Read one source-scoped inventory; no resource is mutated or paused."""
    _resources(connection, bucket, old, new)
    manifest = {
        "version": 2,
        "old": asdict(old),
        "new": asdict(new),
        "inventoried_at": datetime.now(UTC).isoformat(),
        "tables": [],
        "objects": [],
        "tasks": [],
    }
    connection.execute("BEGIN TRANSACTION")
    try:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                "WHERE table_type='BASE TABLE'"
            ).fetchall()
        }
        for table, scope in TABLE_SCOPES.items():
            if table not in existing:
                continue
            manifest["tables"].append(_table_inventory(connection, table))
        connection.execute("ROLLBACK")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    for prefix in PREFIXES:
        manifest["objects"].extend(
            {"key": blob.name, "generation": int(blob.generation)}
            for blob in bucket.list_blobs(prefix=prefix)
        )
    manifest["objects"].sort(key=lambda row: row["key"])
    manifest["tasks"] = sorted(
        task.name for task in tasks.list_tasks(request={"parent": old.queue})
    )
    return manifest


def _validate_manifest(connection, manifest: dict, old: ResourceIds, new: ResourceIds) -> None:
    if (
        manifest.get("version") != 2
        or manifest.get("old") != asdict(old)
        or manifest.get("new") != asdict(new)
    ):
        raise ValueError("manifest resource identities do not match the selected old/new resources")
    tables = manifest.get("tables")
    objects = manifest.get("objects")
    task_names = manifest.get("tasks")
    if not all(isinstance(value, list) for value in (tables, objects, task_names)):
        raise ValueError("manifest inventories must be lists")
    seen_tables, seen_objects, seen_tasks = set(), set(), set()
    for item in tables:
        table = item.get("table")
        if table not in TABLE_SCOPES or table in seen_tables:
            raise ValueError("table is outside the cleanup allowlist or duplicated")
        seen_tables.add(table)
        if item.get("columns") != [
            row[0] for row in connection.execute(f"DESCRIBE {table}").fetchall()
        ]:
            raise ValueError("manifest columns do not match the table")
        if type(item.get("row_count")) is not int or item["row_count"] < 0:
            raise ValueError("invalid manifest row count")
        digest = item.get("fingerprint")
        if (
            not isinstance(digest, list)
            or len(digest) != 2
            or any(not isinstance(v, str) or re.fullmatch(r"[0-9]+", v) is None for v in digest)
        ):
            raise ValueError("invalid manifest fingerprint")
    for row in objects:
        key, generation = row.get("key"), row.get("generation")
        if not isinstance(key, str) or not key.startswith(PREFIXES) or key in seen_objects:
            raise ValueError("object is outside the cleanup allowlist or duplicated")
        if type(generation) is not int or generation <= 0:
            raise ValueError("manifest requires a positive GCS generation")
        seen_objects.add(key)
    for name in task_names:
        if (
            not isinstance(name, str)
            or not name.startswith(old.queue + "/tasks/")
            or not name.removeprefix(old.queue + "/tasks/")
            or "/" in name.removeprefix(old.queue + "/tasks/")
            or name in seen_tasks
        ):
            raise ValueError("task is outside the cleanup allowlist or duplicated")
        seen_tasks.add(name)


def apply_manifest(
    connection,
    bucket,
    tasks,
    manifest: dict,
    *,
    old: ResourceIds,
    new: ResourceIds,
    writers_stopped: bool,
    reviewed_sha256: str,
) -> dict[str, int]:
    """Delete only reviewed identities, with safe replay after a partial external failure."""
    _resources(connection, bucket, old, new)
    if not writers_stopped:
        raise ValueError("old writers must be stopped before cleanup")
    if manifest_sha256(manifest) != reviewed_sha256:
        raise ValueError("manifest differs from the reviewed SHA-256")
    _validate_manifest(connection, manifest, old, new)
    if tasks.get_queue(request={"name": old.queue}).state != 2:
        raise ValueError("the old Cloud Tasks queue must be paused")
    for row in manifest["objects"]:
        blob = bucket.blob(row["key"])
        try:
            blob.reload()
        except NotFound:
            continue
        if int(blob.generation) != row["generation"]:
            raise ValueError(f"GCS generation changed: {row['key']}")
    counts = {"rows": 0, "objects": 0, "tasks": 0}
    connection.execute("BEGIN TRANSACTION")
    try:
        if connection.execute(
            "SELECT count(*) FROM ops.job_lock WHERE expires_at > current_timestamp"
        ).fetchone()[0]:
            raise ValueError("active old writer lease prevents cleanup")
        remaining = []
        for item in manifest["tables"]:
            current = _table_inventory(connection, item["table"])
            if current["row_count"] == 0:
                continue
            if current != item:
                raise ValueError(f"inventoried row changed: {item['table']}")
            remaining.append(item)
        for item in remaining:
            table = item["table"]
            deleted = connection.execute(
                f"DELETE FROM {table} WHERE {TABLE_SCOPES[table]}"
            ).fetchone()[0]
            if deleted != item["row_count"]:
                raise RuntimeError("cleanup row count changed")
            counts["rows"] += deleted
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    for row in manifest["objects"]:
        try:
            bucket.blob(row["key"]).delete(if_generation_match=row["generation"])
            counts["objects"] += 1
        except NotFound:
            pass
    for name in manifest["tasks"]:
        try:
            tasks.delete_task(request={"name": name})
            counts["tasks"] += 1
        except NotFound:
            pass
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inventory-only", action="store_true")
    mode.add_argument("--apply", type=Path, metavar="MANIFEST")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="inventory output under ignored var/ or outside the repository",
    )
    parser.add_argument("--reviewed-sha256")
    parser.add_argument("--old-writers-stopped", action="store_true")
    for prefix in ("old", "new"):
        for name in ("database", "bucket", "queue"):
            parser.add_argument(f"--{prefix}-{name}", required=True)
    args = parser.parse_args(argv)
    old = ResourceIds(args.old_database, args.old_bucket, args.old_queue)
    new = ResourceIds(args.new_database, args.new_bucket, args.new_queue)
    if args.inventory_only and not args.manifest:
        parser.error("--inventory-only requires --manifest")
    if args.apply and (not args.reviewed_sha256 or not args.old_writers_stopped):
        parser.error("--apply requires --reviewed-sha256 and --old-writers-stopped")
    if args.manifest:
        root = Path(__file__).resolve().parents[1]
        output = args.manifest.resolve()
        if output.is_relative_to(root) and not output.is_relative_to(root / "var"):
            parser.error("inventory artifacts inside the repository must be under ignored var/")
    # Clients are created only after command-line validation; inventory has no delete calls.
    from google.cloud import storage, tasks_v2

    connection = connect(WarehouseConfig(old.database, os.environ.get("MOTHERDUCK_TOKEN")))
    try:
        bucket = storage.Client().bucket(old.bucket)
        tasks = tasks_v2.CloudTasksClient()
        if args.inventory_only:
            manifest = inventory_legacy(connection, bucket, tasks, old=old, new=new)
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(args.manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(manifest, output, indent=2)
                output.write("\n")
            print(
                json.dumps(
                    {
                        "manifest": str(args.manifest),
                        "sha256": manifest_sha256(manifest),
                        "rows": sum(item["row_count"] for item in manifest["tables"]),
                        "objects": len(manifest["objects"]),
                        "tasks": len(manifest["tasks"]),
                    }
                )
            )
        else:
            manifest = json.loads(args.apply.read_text())
            counts = apply_manifest(
                connection,
                bucket,
                tasks,
                manifest,
                old=old,
                new=new,
                writers_stopped=args.old_writers_stopped,
                reviewed_sha256=args.reviewed_sha256,
            )
            print(json.dumps(counts))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

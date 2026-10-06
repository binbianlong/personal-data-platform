"""Move Screen Time history through a private local DuckDB snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import google.cloud.storage as storage

from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

if __package__ in (None, ""):
    from migrate_west_raw import read_manifest, timestamp, verify_manifest
else:
    from scripts.migrate_west_raw import read_manifest, timestamp, verify_manifest

_STREAM_SCOPE = "source_stream IN ('app-in-focus', 'app-usage', 'App.InFocus')"
_SOURCE_SCOPE = "source_id = 'screen_time' AND " + _STREAM_SCOPE
_DETAIL_SCOPE = "json_extract_string(details, '$.source_id') = 'screen_time'"
# This list controls both export and replacement; it never includes migration or lease tables.
TABLE_SCOPES = {
    "base.screen_time_event": _STREAM_SCOPE,
    "ops.screen_time_segment": _STREAM_SCOPE,
    "ops.screen_time_record": _STREAM_SCOPE,
    "ops.screen_time_tombstone": _STREAM_SCOPE,
    "ops.screen_time_deletion_match": (
        "tombstone_id IN (SELECT physical_id FROM ops.screen_time_tombstone WHERE "
        + _STREAM_SCOPE
        + ") AND physical_id IN (SELECT physical_id FROM "
        "ops.screen_time_record WHERE " + _STREAM_SCOPE + ")"
    ),
    "ops.ingestion_metadata": _SOURCE_SCOPE,
    "ops.reconciliation_run": _DETAIL_SCOPE,
    "ops.heartbeat": (
        "monitor_name IN ('screen_time_reconciliation', 'screen_time_app_usage_reconciliation') "
        "AND " + _DETAIL_SCOPE
    ),
    "ops.job_run": (
        "job_name IN ('loader:screen_time:app-in-focus', 'loader:screen_time:app-usage', "
        "'loader', 'reconciliation', 'dbt') AND status <> 'running' AND "
        "coalesce(json_extract_string(details, '$.source_id'), 'screen_time') = 'screen_time' "
        "AND NOT contains(lower(coalesce(cast(details AS VARCHAR), '')), 'fitbit')"
    ),
}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_META_TABLE = "main.west_snapshot_metadata"


def _artifact_path(path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    resolved = path.resolve()
    if resolved.is_relative_to(root) and not resolved.is_relative_to(root / "var"):
        raise ValueError("migration artifacts inside the repository must be under ignored var/")


def _columns(connection, table: str) -> list[tuple[str, str]]:
    result = [(row[0], row[1]) for row in connection.execute(f"DESCRIBE {table}").fetchall()]
    if any(not _IDENTIFIER.fullmatch(name) for name, _ in result):
        raise ValueError("snapshot contains an unsupported column identifier")
    return result


def _digest(rows) -> str:
    # A multiset digest verifies every value, including PKs, periods and deletion effects.
    def canonical(value):
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
        if isinstance(value, (tuple, list)):
            return [canonical(item) for item in value]
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        return value

    values = sorted(
        hashlib.sha256(
            json.dumps(
                canonical(row), default=str, separators=(",", ":"), ensure_ascii=True
            ).encode()
        ).digest()
        for row in rows
    )
    return hashlib.sha256(b"".join(values)).hexdigest()


def _insert_batch(connection, table: str, columns: list[str], rows) -> None:
    """Use one parameterized statement per batch instead of a remote call per row."""
    projection = ",".join(f'"{name}"' for name in columns)
    values = ",".join("unnest(?)" for _ in columns)
    connection.execute(
        f"INSERT INTO {table} ({projection}) SELECT {values}",
        [list(column) for column in zip(*rows, strict=True)],
    )


def export_snapshot(source_connection, local_path: Path) -> dict[str, int]:
    """Read a consistent snapshot without changing any source table or migration receipt."""
    _artifact_path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = local_path.with_name(local_path.name + "." + uuid.uuid4().hex + ".tmp")
    local = duckdb.connect(str(temporary))
    os.chmod(temporary, 0o600)
    result = {}
    source_connection.execute("BEGIN TRANSACTION")
    try:
        local.execute("BEGIN TRANSACTION")
        local.execute("CREATE SCHEMA ops; CREATE SCHEMA base")
        local.execute(
            f"CREATE TABLE {_META_TABLE} (table_name VARCHAR PRIMARY KEY, "
            "columns_json VARCHAR NOT NULL, row_count BIGINT NOT NULL, digest VARCHAR NOT NULL)"
        )
        for table, scope in TABLE_SCOPES.items():
            columns = _columns(source_connection, table)
            definitions = ", ".join(f'"{name}" {kind}' for name, kind in columns)
            local.execute(f"CREATE TABLE {table} ({definitions})")
            rows = source_connection.execute(f"SELECT * FROM {table} WHERE {scope}").fetchall()
            for offset in range(0, len(rows), 1000):
                _insert_batch(
                    local, table, [name for name, _ in columns], rows[offset : offset + 1000]
                )
            result[table] = len(rows)
            local.execute(
                f"INSERT INTO {_META_TABLE} VALUES (?, ?, ?, ?)",
                [table, json.dumps(columns), len(rows), _digest(rows)],
            )
        local.execute("COMMIT")
        source_connection.execute("ROLLBACK")
        local.close()
        temporary.replace(local_path)
    except BaseException:
        source_connection.execute("ROLLBACK")
        local.close()
        temporary.unlink(missing_ok=True)
        raise
    return result


def _snapshot(local_path: Path):
    local = duckdb.connect(str(local_path), read_only=True)
    try:
        relations = local.execute(
            "SELECT table_schema || '.' || table_name, table_type FROM information_schema.tables"
        ).fetchall()
        if {name for name, kind in relations} != {*TABLE_SCOPES, _META_TABLE} or any(
            kind != "BASE TABLE" for _, kind in relations
        ):
            raise ValueError("snapshot contains a foreign table or view")
        metadata = local.execute(f"SELECT * FROM {_META_TABLE}").fetchall()
        if len(metadata) != len(TABLE_SCOPES) or {row[0] for row in metadata} != set(TABLE_SCOPES):
            raise ValueError("snapshot allowlist metadata is incomplete")
        for table, encoded_columns, count, digest in metadata:
            columns = _columns(local, table)
            if json.loads(encoded_columns) != [list(column) for column in columns]:
                raise ValueError("snapshot schema changed")
            rows = local.execute(f"SELECT * FROM {table}").fetchall()
            scoped_count = local.execute(
                f"SELECT count(*) FROM {table} WHERE {TABLE_SCOPES[table]}"
            ).fetchone()[0]
            if len(rows) != count or scoped_count != count or _digest(rows) != digest:
                raise ValueError("snapshot has changed or contains foreign source rows")
        return local
    except BaseException:
        local.close()
        raise


def _expected(local, target, raw_manifest: Path) -> dict:
    manifest = read_manifest(raw_manifest)
    mapping = {row["key"]: row for row in manifest["objects"]}
    if any(row["target_generation"] is None for row in mapping.values()):
        raise ValueError("every retained Raw must have a completed copy")
    prepared = {}
    for table in TABLE_SCOPES:
        source_columns = [name for name, _ in _columns(local, table)]
        target_columns = [name for name, _ in _columns(target, table)]
        if set(target_columns) - set(source_columns) - {"retention_started_at"}:
            raise ValueError(f"unsupported target schema for {table}")
        if set(source_columns) - set(target_columns):
            raise ValueError(f"unsupported source schema for {table}")
        rows = []
        for values in local.execute(f"SELECT * FROM {table}").fetchall():
            row = dict(zip(source_columns, values, strict=True))
            if table == "ops.ingestion_metadata":
                origin = row.get("retention_started_at") or row["storage_created_at"]
                copied = mapping.get(row["object_key"])
                if copied:
                    if (
                        row["storage_generation"] != copied["source_generation"]
                        or row["storage_created_at"] != timestamp(copied["source_created_at"])
                        or row["content_sha256"] != copied["content_sha256"]
                        or origin != timestamp(copied["retention_started_at"])
                        or row["source_stream"] != copied["stream"]
                    ):
                        raise ValueError("Raw mapping disagrees with source ingestion metadata")
                    row["storage_created_at"] = timestamp(copied["target_created_at"])
                    row["storage_generation"] = copied["target_generation"]
                    row["retention_started_at"] = timestamp(copied["retention_started_at"])
                    row["retention_expired_at"] = None
                else:
                    if row["retention_expired_at"] is None and (
                        row["status"] != "succeeded"
                        or origin is None
                        or origin > datetime.now(UTC) - timedelta(days=90)
                    ):
                        raise ValueError("live or unfinished ingestion is missing its Raw copy")
                    row["retention_started_at"] = origin
            rows.append(tuple(row.get(name) for name in target_columns))
        prepared[table] = (target_columns, rows)
    return prepared


def _verify_prepared(target, prepared) -> dict[str, int]:
    result = {}
    for table, (columns, expected) in prepared.items():
        projection = ",".join(f'"{name}"' for name in columns)
        actual = target.execute(
            f"SELECT {projection} FROM {table} WHERE {TABLE_SCOPES[table]}"
        ).fetchall()
        if len(actual) != len(expected) or _digest(actual) != _digest(expected):
            raise RuntimeError(f"warehouse verification failed for {table}")
        result[table] = len(actual)
    return result


def verify_snapshot(target_connection, local_path: Path, raw_manifest: Path) -> dict[str, int]:
    local = _snapshot(local_path)
    try:
        return _verify_prepared(
            target_connection, _expected(local, target_connection, raw_manifest)
        )
    finally:
        local.close()


def import_snapshot(target_connection, local_path: Path, raw_manifest: Path) -> dict[str, int]:
    """Atomically replace only the allowlisted source rows; safe to resume after a lost response."""
    local = _snapshot(local_path)
    try:
        prepared = _expected(local, target_connection, raw_manifest)
    finally:
        local.close()
    warehouse = Warehouse(target_connection)
    owner = str(uuid.uuid4())
    deadline = time.monotonic() + 100 * 60
    if not warehouse.acquire_job_lock("loader", owner, lease_seconds=125 * 60):
        raise RuntimeError("another writer owns the loader lease")

    def check_lease():
        if time.monotonic() >= deadline:
            raise RuntimeError("migration exceeded its 100 minute deadline")
        lock = target_connection.execute(
            "SELECT owner_id, expires_at FROM ops.job_lock WHERE job_name='loader'"
        ).fetchone()
        if lock is None or lock[0] != owner or lock[1] <= datetime.now(UTC) + timedelta(minutes=1):
            raise RuntimeError("migration lost its loader lease")

    begun = False
    try:
        target_connection.execute("BEGIN TRANSACTION")
        begun = True
        check_lease()
        # Resolve match ownership before replacing the record/tombstone relations.
        for table in [
            "ops.screen_time_deletion_match",
            *(value for value in TABLE_SCOPES if value != "ops.screen_time_deletion_match"),
        ]:
            target_connection.execute(f"DELETE FROM {table} WHERE {TABLE_SCOPES[table]}")
        for table, (columns, rows) in prepared.items():
            for offset in range(0, len(rows), 1000):
                check_lease()
                _insert_batch(target_connection, table, columns, rows[offset : offset + 1000])
        result = _verify_prepared(target_connection, prepared)
        check_lease()
        target_connection.execute("COMMIT")
        begun = False
        return result
    except BaseException:
        if begun:
            target_connection.execute("ROLLBACK")
        raise
    finally:
        warehouse.release_job_lock("loader", owner)


def _source_connection(database: str, *, source_writers_stopped: bool = False):
    if database.endswith((".duckdb", ".db")):
        return duckdb.connect(database, read_only=True)
    token = os.environ.get("SOURCE_MOTHERDUCK_TOKEN")
    if not token:
        raise ValueError("SOURCE_MOTHERDUCK_TOKEN is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", database):
        raise ValueError("invalid source database name")
    connection = duckdb.connect(
        f"md:{database}", read_only=True, config={"motherduck_token": token}
    )
    try:
        row = connection.execute("SELECT * FROM __md_duckling_id()").fetchone()
        if row is None or not isinstance(row[0], str) or not re.search(r"\.rs\.\d+$", row[0]):
            if not (
                source_writers_stopped
                and row is not None
                and isinstance(row[0], str)
                and row[0].endswith(".rw")
            ):
                raise ValueError("SOURCE_MOTHERDUCK_TOKEN must be a read-scaling token")
            active = connection.execute(
                "SELECT count(*) FROM ops.job_lock WHERE expires_at > current_timestamp"
            ).fetchone()
            if active is None or active[0] != 0:
                raise ValueError("source export refuses active warehouse leases")
        return connection
    except BaseException:
        connection.close()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    for action in ("export", "import", "verify"):
        actions.add_argument(f"--{action}", action="store_true", dest=action.replace("-", "_"))
    parser.add_argument("--source-db")
    parser.add_argument(
        "--source-writers-stopped",
        action="store_true",
        help="Allow a read-only connection without read scaling after stopping every source writer",
    )
    parser.add_argument("--target-db")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--raw-manifest", type=Path)
    args = parser.parse_args(argv)
    if args.export and not args.source_db:
        parser.error("source action requires --source-db")
    if not args.export and (not args.target_db or args.raw_manifest is None):
        parser.error("target action requires --target-db and --raw-manifest")
    if args.source_db == args.target_db:
        parser.error("source and target must be different databases")
    if args.source_writers_stopped and not args.export:
        parser.error("--source-writers-stopped requires --export")
    source = (
        _source_connection(args.source_db, source_writers_stopped=args.source_writers_stopped)
        if args.export
        else None
    )
    target = None
    try:
        if args.export:
            result = export_snapshot(source, args.snapshot)
        else:
            verify_manifest(storage.Client(), args.raw_manifest)
            target = connect(
                WarehouseConfig(args.target_db, os.environ.get("TARGET_MOTHERDUCK_TOKEN"))
            )
            if args.verify:
                result = verify_snapshot(target, args.snapshot, args.raw_manifest)
            else:
                Warehouse(target).migrate(profile="west")
                result = import_snapshot(target, args.snapshot, args.raw_manifest)
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        if source is not None:
            source.close()
        if target is not None:
            target.close()


if __name__ == "__main__":
    raise SystemExit(main())

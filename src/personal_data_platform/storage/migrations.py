"""Apply immutable schema migrations to an empty or initialized database."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from personal_data_platform.storage.motherduck import Warehouse

DEFAULT_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
_REBUILD = "unsupported schema history; rebuild into an empty target database"


def _read_applied(connection: DuckDBPyConnection, checksums: dict[str, str]) -> dict[str, str]:
    columns = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_catalog=current_database() AND table_schema='ops' "
        "AND table_name='schema_migration'"
    ).fetchall()
    applied: dict[str, str] = {}
    if columns:
        if {row[0] for row in columns} != {"migration_id", "checksum", "applied_at"}:
            raise RuntimeError(_REBUILD)
        applied = dict(
            connection.execute("SELECT migration_id, checksum FROM ops.schema_migration").fetchall()
        )
    if not applied:
        existing = connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_catalog=current_database() AND table_schema IN ('base','ops','marts') "
            "AND NOT (table_schema='ops' AND table_name='schema_migration')"
        ).fetchone()
        if existing and existing[0]:
            raise RuntimeError(_REBUILD)
    for migration_id, checksum in applied.items():
        if migration_id not in checksums:
            raise RuntimeError(_REBUILD)
        if checksums[migration_id] != checksum:
            raise RuntimeError(
                f"applied migration changed: {migration_id}; "
                "restore its SQL or rebuild into an empty target database"
            )
    return applied


def apply_migrations(
    warehouse: Warehouse, migrations: Path | None = None, *, allow_upgrade: bool = False
) -> None:
    connection = warehouse.connection
    migrations = migrations or DEFAULT_MIGRATIONS
    paths = sorted(migrations.glob("*.sql"))
    if not paths:
        raise ValueError(f"no migrations found: {migrations}")
    prepared = [(path, path.read_text(encoding="utf-8")) for path in paths]
    checksums = {path.name: hashlib.sha256(sql.encode()).hexdigest() for path, sql in prepared}
    applied = _read_applied(connection, checksums)
    pending = [(path, sql) for path, sql in prepared if path.name not in applied]
    if not pending:
        return

    # Publish a fresh schema atomically at startup. Explicit maintenance keeps
    # each migration and its ledger entry in their own transaction.
    batches = [[migration] for migration in pending] if allow_upgrade else [pending]
    for batch in batches:
        with warehouse._transaction("schema migration"):
            if not applied:
                connection.execute("CREATE SCHEMA IF NOT EXISTS ops")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS ops.schema_migration ("
                    "migration_id VARCHAR PRIMARY KEY, checksum VARCHAR NOT NULL, "
                    "applied_at TIMESTAMPTZ NOT NULL)"
                )
            # A concurrent initializer may have committed since the first read.
            applied = _read_applied(connection, checksums)
            if applied and applied != checksums and not allow_upgrade:
                raise RuntimeError("pending schema migrations: stop writers and run pdp migrate")
            for path, sql in batch:
                if path.name in applied:
                    continue
                connection.execute(sql)
                connection.execute(
                    "INSERT INTO ops.schema_migration VALUES (?, ?, ?)",
                    [path.name, checksums[path.name], datetime.now(UTC)],
                )
                applied[path.name] = checksums[path.name]

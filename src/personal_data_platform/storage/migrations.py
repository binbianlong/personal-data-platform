"""Apply the current immutable schema history to an empty or initialized database."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

DEFAULT_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
_REBUILD = "unsupported schema history; rebuild into an empty target database"


def _read_applied(connection: DuckDBPyConnection, checksums: dict[str, str]) -> dict[str, str]:
    columns = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_catalog=current_database() AND table_schema='ops' "
        "AND table_name='schema_migration'"
    ).fetchall()
    if not columns:
        existing = connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_catalog=current_database() AND table_schema IN ('base','ops')"
        ).fetchone()
        if existing and existing[0]:
            raise RuntimeError(_REBUILD)
        return {}
    if {row[0] for row in columns} != {"migration_id", "checksum", "applied_at"}:
        raise RuntimeError(_REBUILD)
    applied = dict(
        connection.execute("SELECT migration_id, checksum FROM ops.schema_migration").fetchall()
    )
    for migration_id, checksum in applied.items():
        if migration_id not in checksums:
            raise RuntimeError(_REBUILD)
        if checksums[migration_id] != checksum:
            raise RuntimeError(f"applied migration changed: {migration_id}")
    return applied


def apply_migrations(connection: DuckDBPyConnection, migrations: Path | None = None) -> None:
    migrations = migrations or DEFAULT_MIGRATIONS
    paths = sorted(migrations.glob("*.sql"))
    if not paths:
        raise ValueError(f"no migrations found: {migrations}")
    prepared = [(path, path.read_text(encoding="utf-8")) for path in paths]
    checksums = {path.name: hashlib.sha256(sql.encode()).hexdigest() for path, sql in prepared}
    _read_applied(connection, checksums)
    connection.execute("CREATE SCHEMA IF NOT EXISTS ops")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ops.schema_migration (
            migration_id VARCHAR PRIMARY KEY,
            checksum VARCHAR NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL
        )"""
    )
    for path, sql in prepared:
        # Startup precedes the writer lease; recheck receipts another startup
        # may have committed since the initial validation.
        if path.name in _read_applied(connection, checksums):
            continue
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(sql)
            connection.execute(
                "INSERT INTO ops.schema_migration VALUES (?, ?, ?)",
                [path.name, checksums[path.name], datetime.now(UTC)],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

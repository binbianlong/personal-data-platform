"""Apply immutable schema histories with profile and checksum protection."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

DEFAULT_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def apply_migrations(
    connection: DuckDBPyConnection,
    migrations: Path | None = None,
    *,
    profile: Literal["legacy", "west"] = "west",
) -> None:
    if profile not in ("legacy", "west"):
        raise ValueError("migration profile must be legacy or west")
    migrations = migrations or (
        DEFAULT_MIGRATIONS / "west" if profile == "west" else DEFAULT_MIGRATIONS
    )
    paths = sorted(migrations.glob("*.sql"))
    if not paths:
        raise ValueError(f"no migrations found: {migrations}")
    migration_ids = {path.name for path in paths}
    if (profile == "legacy" and "003_fitbit_baseline.sql" in migration_ids) or (
        profile == "west"
        and migration_ids.intersection({"003_fitbit.sql", "004_fitbit_acquisition.sql"})
    ):
        raise RuntimeError("migration path does not match the selected profile")
    columns = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_catalog=current_database() AND table_schema='ops' "
        "AND table_name='schema_migration'"
    ).fetchall()
    if columns:
        if ("schema_profile",) in columns:
            profiles = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT schema_profile FROM ops.schema_migration"
                ).fetchall()
            }
        else:
            profiles = {"legacy"}
        if profiles and profiles != {profile}:
            raise RuntimeError(f"migration profile mismatch: requested {profile}")
    # Validate every known applied checksum before executing any new SQL.
    prepared = [(path, path.read_text(encoding="utf-8")) for path in paths]
    checksums = {path.name: hashlib.sha256(sql.encode()).hexdigest() for path, sql in prepared}
    applied = (
        dict(
            connection.execute("SELECT migration_id, checksum FROM ops.schema_migration").fetchall()
        )
        if columns
        else {}
    )
    for migration_id, checksum in applied.items():
        if migration_id in checksums and checksums[migration_id] != checksum:
            raise RuntimeError(f"applied migration changed: {migration_id}")
    connection.execute("CREATE SCHEMA IF NOT EXISTS ops")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ops.schema_migration (
            migration_id VARCHAR PRIMARY KEY,
            checksum VARCHAR NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL,
            schema_profile VARCHAR NOT NULL DEFAULT 'legacy'
        )
        """
    )
    if columns and ("schema_profile",) not in columns:
        connection.execute(
            "ALTER TABLE ops.schema_migration ADD COLUMN schema_profile VARCHAR DEFAULT 'legacy'"
        )
    for path, sql in prepared:
        if path.name in applied:
            continue
        # Startup migration precedes the job lease. Another startup may have
        # committed this file since the initial ledger snapshot.
        existing = connection.execute(
            "SELECT checksum FROM ops.schema_migration WHERE migration_id = ?", [path.name]
        ).fetchone()
        if existing:
            if existing[0] != checksums[path.name]:
                raise RuntimeError(f"applied migration changed: {path.name}")
            continue
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(sql)
            connection.execute(
                "INSERT INTO ops.schema_migration "
                "(migration_id, checksum, applied_at, schema_profile) VALUES (?, ?, ?, ?)",
                [path.name, checksums[path.name], datetime.now(UTC), profile],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

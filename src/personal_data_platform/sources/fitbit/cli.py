"""Explicit schema, daily collection and manual range command boundaries."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

from .models import DATA_TYPES, parse_time


def configure(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="fitbit_command", required=True)
    migrate = commands.add_parser("migrate", help="apply additive schema migrations explicitly")
    migrate.add_argument("--database")
    sync = commands.add_parser(
        "sync", help="repair a half-open physical range; dates use Asia/Tokyo"
    )
    sync.add_argument("--from", dest="start", required=True)
    sync.add_argument("--to", dest="end", required=True)
    sync.add_argument("--data-type", action="append", choices=DATA_TYPES)
    commands.add_parser("daily", help="collect completed days and recover interrupted batches")


def _database(value: str | None) -> Warehouse:
    if value is not None and not value.endswith((".duckdb", ".db")):
        raise ValueError("--database must name a local .duckdb or .db file")
    return Warehouse(connect(WarehouseConfig(value) if value else WarehouseConfig.from_env()))


def _physical(value: str) -> datetime:
    if len(value) == 10:
        return datetime.combine(
            date.fromisoformat(value), datetime.min.time(), ZoneInfo("Asia/Tokyo")
        )
    return parse_time(value)


def run(args: argparse.Namespace) -> int:
    command = args.fitbit_command
    if command == "migrate":
        migration_warehouse = _database(args.database)
        try:
            migration_warehouse.migrate()
        finally:
            migration_warehouse.close()
        return 0
    from .runtime import run_daily_from_env, run_sync_from_env

    if command == "daily":
        result = run_daily_from_env()
    elif command == "sync":
        result = run_sync_from_env(
            start=_physical(args.start),
            end=_physical(args.end),
            data_types=tuple(args.data_type or DATA_TYPES),
        )
    else:
        raise ValueError("unsupported Fitbit command")
    print(json.dumps(asdict(result), sort_keys=True))
    return int(result.status == "failed")

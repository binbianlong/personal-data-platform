"""Explicit schema and webhook-runtime command boundaries."""

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
    migrate.add_argument("--profile", choices=("legacy", "west"))
    sync = commands.add_parser(
        "sync", help="repair a half-open physical range; dates use Asia/Tokyo"
    )
    sync.add_argument("--from", dest="start", required=True)
    sync.add_argument("--to", dest="end", required=True)
    sync.add_argument("--data-type", action="append", choices=DATA_TYPES)
    ingest = commands.add_parser(
        "ingest-notifications", help="pull, acquire and commit notifications before ack"
    )
    ingest.add_argument("--max-messages", type=int, default=500)
    ingest.add_argument("--collect-seconds", type=int, default=120)
    ingest.add_argument("--timeout-seconds", type=int, default=3000)
    commands.add_parser("serve", help="start the webhook and authenticated worker service")
    commands.add_parser("repair", help="recover pending receipts and Raw when repair is enabled")


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
            from personal_data_platform.config import schema_profile

            migration_warehouse.migrate(profile=args.profile or schema_profile())
        finally:
            migration_warehouse.close()
        return 0
    from .runtime import run_repair_from_env, run_serve_from_env, run_sync_from_env

    if command == "ingest-notifications":
        from .runtime import run_notification_job

        result = run_notification_job(
            max_messages=args.max_messages,
            collect_seconds=args.collect_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        print(json.dumps(asdict(result), sort_keys=True, default=str))
        return int(bool(result.failed_scopes))
    if command == "serve":
        return run_serve_from_env()
    if command == "sync":
        return run_sync_from_env(
            start=_physical(args.start),
            end=_physical(args.end),
            data_types=tuple(args.data_type or DATA_TYPES),
        )
    if command == "repair":
        repaired = run_repair_from_env()
        print(json.dumps(asdict(repaired), sort_keys=True))
        return int(bool(repaired.failed_count or repaired.at_risk_count))
    raise ValueError("unsupported Fitbit command")

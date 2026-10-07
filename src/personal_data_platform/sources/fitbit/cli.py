"""Webhook and synchronization commands."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .models import DATA_TYPES, parse_time


def configure(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="fitbit_command", required=True)
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
    commands.add_parser("serve", help="start the webhook notification receiver")


def _physical(value: str) -> datetime:
    if len(value) == 10:
        return datetime.combine(
            date.fromisoformat(value), datetime.min.time(), ZoneInfo("Asia/Tokyo")
        )
    return parse_time(value)


def run(args: argparse.Namespace) -> int:
    command = args.fitbit_command
    from .runtime import run_serve_from_env, run_sync_from_env

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
    raise ValueError("unsupported Fitbit command")

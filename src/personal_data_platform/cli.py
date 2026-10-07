"""Command-line interface for local collection and cloud runtime jobs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdp", description="Run Personal Data Platform")
    commands = parser.add_subparsers(dest="command", required=True)

    migrate = commands.add_parser("migrate", help="initialize or update the current schema")
    migrate.add_argument("--database", help="local .duckdb or .db file; defaults to MotherDuck")

    from personal_data_platform.sources.fitbit.cli import configure as configure_fitbit
    from personal_data_platform.sources.screen_time.cli import configure as configure_screen_time

    configure_fitbit(
        commands.add_parser("fitbit", help="receive and synchronize Fitbit health data")
    )

    configure_screen_time(commands.add_parser("screen-time", help="inspect or collect Screen Time"))

    loader = commands.add_parser("loader", help="load pending GCS Raw into MotherDuck")
    dbt = commands.add_parser("dbt", help="apply analytics models")
    commands.add_parser("reconciliation", help="run all daily ingestion, repair and audit stages")
    commands.add_parser("preflight", help="validate cloud runtime connectivity")
    rebuild = commands.add_parser("rebuild", help="rebuild a scratch MotherDuck database")
    rebuild_mode = rebuild.add_mutually_exclusive_group(required=True)
    rebuild_mode.add_argument("--dry-run", action="store_true")
    rebuild_mode.add_argument("--target-db")
    rebuild.add_argument(
        "--allow-partial-history",
        action="store_true",
        help="acknowledge that only currently retained Raw can be rebuilt",
    )
    for command in (loader, dbt, rebuild):
        command.add_argument("--source", dest="source_id", help="registered data source")
        command.add_argument("--stream", help="registered stream within the selected source")
    for command in (loader, rebuild):
        command.add_argument(
            "--all-streams",
            action="store_true",
            help="process every registered stream for --source",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a concrete command and convert runtime failures to a non-zero exit."""
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except Exception as error:
        _print_error(error)
        return 1


def _print_error(error: Exception) -> None:
    if isinstance(error, ExceptionGroup):
        print(f"error: {error.message}", file=sys.stderr)
        for nested in error.exceptions:
            _print_error(nested)
    else:
        print(f"error: {error}", file=sys.stderr)


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "migrate":
        from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

        if args.database is not None and not args.database.endswith((".duckdb", ".db")):
            raise ValueError("--database must name a local .duckdb or .db file")
        config = WarehouseConfig(args.database) if args.database else WarehouseConfig.from_env()
        warehouse = Warehouse(connect(config))
        try:
            warehouse.migrate()
        finally:
            warehouse.close()
        return 0
    if args.command == "fitbit":
        from personal_data_platform.sources.fitbit.cli import run

        return run(args)
    if args.command == "screen-time":
        from personal_data_platform.sources.screen_time.cli import run

        return run(args)
    if args.command == "loader":
        from personal_data_platform.loader.job import run_loader_from_env

        return _run_job(
            run_loader_from_env,
            source_id=args.source_id,
            stream=args.stream,
            **({"all_streams": True} if args.all_streams else {}),
        )
    if args.command == "dbt":
        from personal_data_platform.dbt_runner import run_dbt_from_env

        return _run_job(run_dbt_from_env, source_id=args.source_id, stream=args.stream)
    if args.command == "reconciliation":
        from personal_data_platform.reconciliation.job import run_reconciliation_from_env

        return _run_job(run_reconciliation_from_env)
    if args.command == "preflight":
        from personal_data_platform.preflight import run_preflight_from_env

        return _run_job(run_preflight_from_env)
    if args.command == "rebuild":
        from personal_data_platform.recovery.rebuild import run_rebuild_from_env

        return _run_job(
            run_rebuild_from_env,
            dry_run=args.dry_run,
            target_db=args.target_db,
            allow_partial_history=args.allow_partial_history,
            source_id=args.source_id,
            stream=args.stream,
            **({"all_streams": True} if args.all_streams else {}),
        )
    raise RuntimeError(f"unsupported command: {args.command}")


def _run_job[**P](function: Callable[P, int | None], *args: P.args, **kwargs: P.kwargs) -> int:
    result = function(*args, **kwargs)
    if result is None:
        return 0
    if isinstance(result, bool) or not isinstance(result, int):
        raise TypeError("runtime job must return an integer exit status or None")
    return result

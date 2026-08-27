"""Command-line interface for local collection and cloud runtime jobs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from personal_data_platform.collectors.screen_time import (
    BiomeScreenTimeSource,
    ScreenTimeCollector,
)
from personal_data_platform.collectors.state import CollectorState
from personal_data_platform.config import B2Config, CollectorConfig, ConfigurationError
from personal_data_platform.raw.screen_time import build_device_key
from personal_data_platform.storage.b2 import B2RawRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdp", description="Run Personal Data Platform")
    commands = parser.add_subparsers(dest="command", required=True)

    screen_time = commands.add_parser("screen-time", help="inspect or collect Screen Time")
    screen_time_commands = screen_time.add_subparsers(dest="screen_time_command", required=True)
    screen_time_commands.add_parser("devices", help="list pseudonymized iPhone devices")
    screen_time_commands.add_parser("doctor", help="diagnose collector configuration and access")
    collect = screen_time_commands.add_parser("collect", help="collect App.InFocus segments")
    collection_mode = collect.add_mutually_exclusive_group(required=True)
    collection_mode.add_argument(
        "--once",
        action="store_true",
        help="perform one complete scan and exit",
    )
    collection_mode.add_argument(
        "--watch",
        action="store_true",
        help="repeat complete scans at the configured interval",
    )
    launch_agent = screen_time_commands.add_parser(
        "launch-agent",
        help="write an unloaded macOS LaunchAgent plist",
    )
    launch_agent.add_argument("--output", required=True, type=Path)
    launch_agent.add_argument("--project-root", default=Path.cwd(), type=Path)
    launch_agent.add_argument(
        "--python-executable",
        default=Path(sys.executable),
        type=Path,
    )
    launch_agent.add_argument("--log-directory", type=Path)

    commands.add_parser("loader", help="load pending B2 Raw into MotherDuck")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a concrete command and convert runtime failures to a non-zero exit."""
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "screen-time":
        if args.screen_time_command == "devices":
            return _run_devices()
        if args.screen_time_command == "doctor":
            return _run_doctor()
        if args.screen_time_command == "collect":
            return _run_collect(watch=args.watch)
        if args.screen_time_command == "launch-agent":
            return _write_launch_agent(
                output_path=args.output,
                project_root=args.project_root,
                python_executable=args.python_executable,
                log_directory=args.log_directory,
            )
        raise RuntimeError(f"unsupported Screen Time command: {args.screen_time_command}")
    if args.command == "loader":
        from personal_data_platform.loader.job import run_loader_from_env

        return _run_job(run_loader_from_env)
    raise RuntimeError(f"unsupported command: {args.command}")


def _run_devices() -> int:
    config = CollectorConfig.from_env(require_b2=False, require_allowlist=False)
    source = _source(config)
    for device in source.list_iphone_devices():
        device_key = build_device_key(config.pseudonym_key, device.identifier)
        print(
            json.dumps(
                {
                    "device_key": device_key,
                    "name": device.name,
                    "model": device.model,
                    "allowed": device_key in config.device_allowlist,
                    "stream_directory_exists": source.device_directory(device).is_dir(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


def _run_doctor() -> int:
    checks: list[tuple[str, bool, str]] = []
    try:
        config = CollectorConfig.from_env(require_b2=False, require_allowlist=False)
    except ConfigurationError as error:
        _print_check("collector secret", False, str(error))
        return 1

    source = _source(config)
    try:
        devices = source.list_iphone_devices()
        checks.append(("Biome sync.db", True, f"platform=2 devices: {len(devices)}"))
    except Exception as error:
        devices = []
        checks.append(("Biome sync.db", False, str(error)))

    checks.append(
        (
            "device allowlist",
            bool(config.device_allowlist),
            f"configured keys: {len(config.device_allowlist)}",
        )
    )
    discovered_keys = {
        build_device_key(config.pseudonym_key, device.identifier) for device in devices
    }
    matched_keys = discovered_keys & config.device_allowlist
    checks.append(
        (
            "allowlisted devices",
            bool(matched_keys),
            f"discovered matches: {len(matched_keys)}",
        )
    )
    readable_directories = sum(
        1
        for device in devices
        if build_device_key(config.pseudonym_key, device.identifier) in config.device_allowlist
        and source.device_directory(device).is_dir()
    )
    checks.append(
        (
            "App.InFocus remote",
            bool(matched_keys) and readable_directories == len(matched_keys),
            f"readable allowlisted directories: {readable_directories}/{len(matched_keys)}",
        )
    )
    state_parent = _nearest_existing_parent(config.state_db_path.parent)
    checks.append(
        (
            "collector state",
            os.access(state_parent, os.W_OK),
            f"state directory: {config.state_db_path.parent}",
        )
    )
    try:
        B2Config.from_env()
    except ConfigurationError as error:
        checks.append(("B2 configuration", False, str(error)))
    else:
        checks.append(
            (
                "B2 configuration",
                True,
                "credentials loaded; upload is verified by collect --once",
            )
        )

    for label, ok, detail in checks:
        _print_check(label, ok, detail)
    return 0 if all(ok for _, ok, _ in checks) else 1


def _run_collect(*, watch: bool) -> int:
    config = CollectorConfig.from_env()
    if config.b2 is None:
        raise ConfigurationError("B2 configuration is required for collection")
    collector = ScreenTimeCollector(
        source=_source(config),
        state=CollectorState(config.state_db_path),
        uploader=B2RawRepository.from_config(config.b2),
        pseudonym_key=config.pseudonym_key,
        allowed_device_keys=config.device_allowlist,
    )
    if not watch:
        _print_collection_stats(collector.collect_once())
        return 0

    interval = _positive_seconds(os.environ.get("PDP_COLLECTOR_POLL_SECONDS", "300"))
    try:
        while True:
            _print_collection_stats(collector.collect_once())
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _write_launch_agent(
    *,
    output_path: Path,
    project_root: Path,
    python_executable: Path,
    log_directory: Path | None,
) -> int:
    from personal_data_platform.launchd import LaunchAgentSettings, write_launch_agent

    settings = LaunchAgentSettings.from_env(
        project_root=project_root,
        python_executable=python_executable,
        log_directory=log_directory,
    )
    print(write_launch_agent(settings, output_path))
    return 0


def _print_collection_stats(stats: Any) -> None:
    print(
        json.dumps(
            {
                "devices": stats.devices,
                "segments": stats.segments,
                "uploaded": stats.uploaded,
                "skipped": stats.skipped,
                "retried": stats.retried,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def _positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise ConfigurationError("PDP_COLLECTOR_POLL_SECONDS must be numeric") from error
    if parsed < 10:
        raise ConfigurationError("PDP_COLLECTOR_POLL_SECONDS must be at least 10 seconds")
    return parsed


def _source(config: CollectorConfig) -> BiomeScreenTimeSource:
    return BiomeScreenTimeSource(
        sync_db_path=config.sync_db_path,
        remote_dir=config.app_in_focus_remote_dir,
    )


def _run_job(function: Callable[..., Any], **kwargs: Any) -> int:
    result = function(**kwargs)
    if result is None:
        return 0
    if isinstance(result, bool) or not isinstance(result, int):
        raise TypeError("runtime job must return an integer exit status or None")
    return result


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _print_check(label: str, ok: bool, detail: str) -> None:
    status = "ok" if ok else "error"
    print(f"{status}\t{label}\t{detail}")

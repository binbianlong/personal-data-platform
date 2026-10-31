"""Command registration and diagnostics for local Screen Time collection."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from personal_data_platform.biome.raw import build_device_key
from personal_data_platform.config import ConfigurationError
from personal_data_platform.sources.mac_screen_time import runtime
from personal_data_platform.sources.mac_screen_time.config import CollectorConfig


def configure(parser: argparse.ArgumentParser) -> None:
    source_commands = parser.add_subparsers(dest="source_command", required=True)
    source_commands.add_parser("devices", help="list pseudonymized Screen Time devices")
    source_commands.add_parser("doctor", help="diagnose collector configuration and access")
    inspect_mac = source_commands.add_parser(
        "inspect-mac", help="inspect completed local Mac AppUsage segments without saving them"
    )
    inspect_mac.add_argument("--directory", type=Path)
    collect = source_commands.add_parser("collect", help="collect Screen Time segments")
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
    launch_agent = source_commands.add_parser(
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


def run(args: argparse.Namespace) -> int:
    if args.source_command == "devices":
        return _run_devices()
    if args.source_command == "doctor":
        return _run_doctor()
    if args.source_command == "inspect-mac":
        from .mac_inspection import DEFAULT_MAC_DIRECTORY, inspect_mac_directory

        print(
            json.dumps(
                inspect_mac_directory(args.directory or DEFAULT_MAC_DIRECTORY),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.source_command == "collect":
        return runtime.collect(watch=args.watch)
    if args.source_command == "launch-agent":
        return _write_launch_agent(
            output_path=args.output,
            project_root=args.project_root,
            python_executable=args.python_executable,
            log_directory=args.log_directory,
        )
    raise RuntimeError(f"unsupported Screen Time command: {args.source_command}")


def _run_devices() -> int:
    config = CollectorConfig.from_env(require_allowlist=False)
    source = runtime.collector_source(config)
    allowed = frozenset({config.device_key}) if config.device_key else frozenset()
    for device in source.list_devices():
        device_key = build_device_key(config.pseudonym_key, device.identifier)
        print(
            json.dumps(
                {
                    "device_key": device_key,
                    "platform": source.platform,
                    "name": device.name,
                    "model": device.model,
                    "allowed": device_key in allowed,
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
        config = CollectorConfig.from_env(require_allowlist=False)
    except ConfigurationError as error:
        _print_check("collector secret", False, str(error))
        return 1

    source = runtime.collector_source(config)
    allowed = frozenset({config.device_key}) if config.device_key else frozenset()
    try:
        devices = source.list_devices()
        selected = [
            device
            for device in devices
            if build_device_key(config.pseudonym_key, device.identifier) in allowed
        ]
        checks.append(
            ("configured devices", bool(selected), f"discovered matches: {len(selected)}")
        )
        checks.append(
            (
                source.segment_stream,
                bool(selected)
                and all(source.device_directory(device).is_dir() for device in selected),
                "segment directories",
            )
        )
    except Exception as error:
        checks.append(("Biome discovery", False, str(error)))
    state_parent = _nearest_existing_parent(config.state_db_path.parent)
    checks.append(
        (
            "collector state",
            os.access(state_parent, os.W_OK),
            f"state directory: {config.state_db_path.parent}",
        )
    )
    try:
        runtime.warehouse_config()
    except ConfigurationError as error:
        checks.append(("MotherDuck configuration", False, str(error)))
    else:
        checks.append(
            (
                "MotherDuck configuration",
                True,
                "dedicated collector token loaded; connection is verified by collect --once",
            )
        )

    for label, ok, detail in checks:
        _print_check(label, ok, detail)
    return 0 if all(ok for _, ok, _ in checks) else 1


def _write_launch_agent(
    *,
    output_path: Path,
    project_root: Path,
    python_executable: Path,
    log_directory: Path | None,
) -> int:
    from personal_data_platform.sources.mac_screen_time.launchd import (
        LaunchAgentSettings,
        write_launch_agent,
    )

    settings = LaunchAgentSettings.from_env(
        project_root=project_root,
        python_executable=python_executable,
        log_directory=log_directory,
    )
    print(write_launch_agent(settings, output_path))
    return 0


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _print_check(label: str, ok: bool, detail: str) -> None:
    status = "ok" if ok else "error"
    print(f"{status}\t{label}\t{detail}")

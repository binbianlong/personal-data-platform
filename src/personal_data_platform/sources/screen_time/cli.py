"""Command registration and diagnostics for local Screen Time collection."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from personal_data_platform.config import ConfigurationError
from personal_data_platform.sources.screen_time import runtime
from personal_data_platform.sources.screen_time.collector import CollectorSourceError
from personal_data_platform.sources.screen_time.config import CollectorConfig
from personal_data_platform.sources.screen_time.raw import build_device_key


def configure(parser: argparse.ArgumentParser) -> None:
    screen_time_commands = parser.add_subparsers(dest="screen_time_command", required=True)
    screen_time_commands.add_parser("devices", help="list pseudonymized Screen Time devices")
    screen_time_commands.add_parser("doctor", help="diagnose collector configuration and access")
    inspect_mac = screen_time_commands.add_parser(
        "inspect-mac", help="inspect completed local Mac App.InFocus segments without saving them"
    )
    inspect_mac.add_argument(
        "--directory", type=Path, help="App.InFocus/local directory (defaults to this Mac's Biome)"
    )
    collect = screen_time_commands.add_parser("collect", help="collect Screen Time segments")
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


def run(args: argparse.Namespace) -> int:
    if args.screen_time_command == "devices":
        return _run_devices()
    if args.screen_time_command == "doctor":
        return _run_doctor()
    if args.screen_time_command == "inspect-mac":
        return _run_inspect_mac(directory=args.directory)
    if args.screen_time_command == "collect":
        return runtime.collect(watch=args.watch)
    if args.screen_time_command == "launch-agent":
        return _write_launch_agent(
            output_path=args.output,
            project_root=args.project_root,
            python_executable=args.python_executable,
            log_directory=args.log_directory,
        )
    raise RuntimeError(f"unsupported Screen Time command: {args.screen_time_command}")


def _run_devices() -> int:
    config = CollectorConfig.from_env(require_allowlist=False)
    for source, allowed in (
        (runtime.iphone_source(config), config.device_allowlist),
        (
            runtime.mac_source(config),
            frozenset({config.mac_device_key}) if config.mac_device_key else frozenset(),
        ),
    ):
        try:
            devices = source.list_devices()
        except CollectorSourceError as error:
            if source.platform != "macos" or config.mac_device_key:
                raise
            print(f"warning: local Mac discovery unavailable: {error}", file=sys.stderr)
            continue
        for device in devices:
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


def _run_inspect_mac(*, directory: Path | None) -> int:
    from personal_data_platform.sources.screen_time.mac_inspection import (
        DEFAULT_MAC_DIRECTORY,
        inspect_mac_directory,
    )

    print(
        json.dumps(
            inspect_mac_directory(directory or DEFAULT_MAC_DIRECTORY),
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

    if config.device_allowlist:
        source = runtime.iphone_source(config)
        try:
            devices = source.list_iphone_devices()
            checks.append(("Biome sync.db", True, f"platform=2 devices: {len(devices)}"))
        except Exception as error:
            devices = []
            checks.append(("Biome sync.db", False, str(error)))

        discovered_keys = {
            build_device_key(config.pseudonym_key, device.identifier) for device in devices
        }
        matched_keys = discovered_keys & config.device_allowlist
        checks.append(
            (
                "allowlisted iPhone devices",
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
    if config.mac_device_key:
        mac_source = runtime.mac_source(config)
        try:
            mac_devices = mac_source.list_devices()
            mac_key = build_device_key(config.pseudonym_key, mac_devices[0].identifier)
            checks.append(
                ("local Mac device key", mac_key == config.mac_device_key, "platform=3 me=1")
            )
            checks.append(
                (
                    "ScreenTime.AppUsage local",
                    mac_source.device_directory(mac_devices[0]).is_dir(),
                    "local segment directory",
                )
            )
        except Exception as error:
            checks.append(("local Mac", False, str(error)))
    if not (config.device_allowlist or config.mac_device_key):
        checks.append(("Screen Time device keys", False, "no configured iPhone or Mac key"))
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
    from personal_data_platform.sources.screen_time.launchd import (
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

"""Command registration and local Screen Time collection operations."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from personal_data_platform.config import (
    ConfigurationError,
    GCSConfig,
)
from personal_data_platform.sources.screen_time.collector import (
    BiomeMacAppUsageSource,
    BiomeScreenTimeSource,
    CollectionStats,
    CollectorSourceError,
    CompressedRawUploader,
    ScreenTimeCollector,
)
from personal_data_platform.sources.screen_time.config import CollectorADCConfig, CollectorConfig
from personal_data_platform.sources.screen_time.raw import (
    APP_IN_FOCUS_STREAM,
    APP_USAGE_STREAM,
    CollectorDeviceManifest,
    build_device_key,
)
from personal_data_platform.sources.screen_time.state import CollectorState
from personal_data_platform.sources.screen_time.storage import ScreenTimeGCSRepository


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
        return _run_collect(watch=args.watch)
    if args.screen_time_command == "launch-agent":
        return _write_launch_agent(
            output_path=args.output,
            project_root=args.project_root,
            python_executable=args.python_executable,
            log_directory=args.log_directory,
        )
    raise RuntimeError(f"unsupported Screen Time command: {args.screen_time_command}")


def _run_devices() -> int:
    config = CollectorConfig.from_env(require_gcs=False, require_allowlist=False)
    for source, allowed in (
        (_source(config), config.device_allowlist),
        (
            _mac_source(config),
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
        config = CollectorConfig.from_env(require_gcs=False, require_allowlist=False)
    except ConfigurationError as error:
        _print_check("collector secret", False, str(error))
        return 1

    if config.device_allowlist:
        source = _source(config)
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
        mac_source = _mac_source(config)
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
        GCSConfig.from_env()
    except ConfigurationError as error:
        checks.append(("GCS configuration", False, str(error)))
    else:
        checks.append(
            (
                "GCS configuration",
                True,
                "project and bucket loaded; upload is verified by collect --once",
            )
        )
    try:
        adc = CollectorADCConfig.from_env()
    except ConfigurationError as error:
        checks.append(("collector ADC", False, str(error)))
    else:
        checks.append(
            (
                "collector ADC",
                True,
                f"impersonates {adc.service_account_email}",
            )
        )

    for label, ok, detail in checks:
        _print_check(label, ok, detail)
    return 0 if all(ok for _, ok, _ in checks) else 1


def _run_collect(*, watch: bool) -> int:
    adc = CollectorADCConfig.from_env()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc.credentials_path)
    config = CollectorConfig.from_env()
    if config.gcs is None:
        raise ConfigurationError("GCS configuration is required for collection")
    state = CollectorState(config.state_db_path)
    uploader = ScreenTimeGCSRepository.from_config(config.gcs)
    collectors = []
    if config.device_allowlist:
        collectors.append(
            ScreenTimeCollector(
                source=_source(config),
                state=state,
                uploader=uploader,
                pseudonym_key=config.pseudonym_key,
                allowed_device_keys=config.device_allowlist,
            )
        )
    if config.mac_device_key:
        collectors.append(
            ScreenTimeCollector(
                source=_mac_source(config),
                state=state,
                uploader=uploader,
                pseudonym_key=config.pseudonym_key,
                allowed_device_keys=frozenset({config.mac_device_key}),
            )
        )
    inactive_streams = tuple(
        stream
        for stream, enabled in (
            (APP_IN_FOCUS_STREAM, bool(config.device_allowlist)),
            (APP_USAGE_STREAM, config.mac_device_key is not None),
        )
        if not enabled
    )
    if not watch:
        _print_collection_stats(
            _collect_all(collectors, inactive_streams=inactive_streams, inactive_uploader=uploader)
        )
        return 0

    interval = _positive_seconds(os.environ.get("PDP_COLLECTOR_POLL_SECONDS", "1800"))
    try:
        while True:
            _print_collection_stats(
                _collect_all(
                    collectors, inactive_streams=inactive_streams, inactive_uploader=uploader
                )
            )
            inactive_streams = ()
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


def _print_collection_stats(stats: CollectionStats) -> None:
    print(
        json.dumps(
            {
                "devices": stats.devices,
                "segments": stats.segments,
                "uploaded": stats.uploaded,
                "skipped": stats.skipped,
                "retried": stats.retried,
                "deferred": stats.deferred,
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


def _mac_source(config: CollectorConfig) -> BiomeMacAppUsageSource:
    if config.mac_app_usage_local_dir is None:
        raise ConfigurationError("Mac AppUsage directory is not configured")
    return BiomeMacAppUsageSource(
        sync_db_path=config.sync_db_path,
        local_dir=config.mac_app_usage_local_dir,
    )


def _collect_all(
    collectors: Sequence[ScreenTimeCollector],
    *,
    inactive_streams: Sequence[str] = (),
    inactive_uploader: CompressedRawUploader | None = None,
) -> CollectionStats:
    """Attempt each configured stream and report failure after all attempts."""
    results: list[CollectionStats] = []
    failures: list[Exception] = []
    for collector in collectors:
        try:
            results.append(collector.collect_once())
        except Exception as error:
            failures.append(RuntimeError(f"{collector.stream}: {error}"))
    if inactive_streams and inactive_uploader is None:
        raise ValueError("inactive_uploader is required for inactive streams")
    for stream in inactive_streams:
        assert inactive_uploader is not None
        try:
            inactive_uploader.put_device_manifest(
                CollectorDeviceManifest((), datetime.now(UTC), stream=stream)
            )
        except Exception as error:
            failures.append(RuntimeError(f"{stream} deactivation: {error}"))
    if failures:
        raise ExceptionGroup("Screen Time collection failed", failures)
    return CollectionStats(
        devices=sum(result.devices for result in results),
        segments=sum(result.segments for result in results),
        uploaded=sum(result.uploaded for result in results),
        skipped=sum(result.skipped for result in results),
        retried=sum(result.retried for result in results),
        deferred=sum(result.deferred for result in results),
    )


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _print_check(label: str, ok: bool, detail: str) -> None:
    status = "ok" if ok else "error"
    print(f"{status}\t{label}\t{detail}")

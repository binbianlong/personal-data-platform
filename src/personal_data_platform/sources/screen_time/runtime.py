"""Local Screen Time collection and warehouse loading operations."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from personal_data_platform.config import ConfigurationError
from personal_data_platform.loader.job import (
    LOADER_LEASE_SECONDS,
    JobAlreadyRunning,
    run_loader_objects,
)
from personal_data_platform.sources.contracts import list_source_raw
from personal_data_platform.sources.registry import get_sources
from personal_data_platform.sources.screen_time.collector import (
    BiomeMacAppUsageSource,
    BiomeScreenTimeSource,
    CollectionStats,
    CollectorSourceError,
    ScreenTimeCollector,
)
from personal_data_platform.sources.screen_time.collector_state import CollectorScan, CollectorState
from personal_data_platform.sources.screen_time.config import CollectorConfig, _env_or_keychain
from personal_data_platform.sources.screen_time.raw import APP_IN_FOCUS_STREAM, APP_USAGE_STREAM
from personal_data_platform.sources.screen_time.storage import ScreenTimeLocalRepository
from personal_data_platform.sources.screen_time.warehouse import finish_collection, prune_receipts
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


def collect(*, watch: bool) -> int:
    config = CollectorConfig.from_env()
    state = CollectorState(config.state_db_path)
    collectors = []
    if config.device_allowlist:
        collectors.append(
            ScreenTimeCollector(
                source=iphone_source(config),
                state=state,
                pseudonym_key=config.pseudonym_key,
                allowed_device_keys=config.device_allowlist,
            )
        )
    if config.mac_device_key:
        collectors.append(
            ScreenTimeCollector(
                source=mac_source(config),
                state=state,
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
    schema_initialized = False

    def collect_and_load() -> None:
        nonlocal schema_initialized
        stats = collect_all(
            collectors,
            inactive_streams=inactive_streams,
            inactive_state=state,
        )
        warehouse = Warehouse(connect(warehouse_config()))
        try:
            if not schema_initialized:
                warehouse.migrate()
                schema_initialized = True
            pending = len(state.pending())
            load_pending(state, warehouse, publish_success=not stats.deferred)
            _print_collection_stats(stats)
            print(
                json.dumps(
                    {"loaded": pending - len(state.pending()), "pending": len(state.pending())}
                ),
                flush=True,
            )
        finally:
            warehouse.close()
        if stats.deferred:
            raise CollectorSourceError(
                f"{stats.deferred} incomplete Screen Time snapshots deferred"
            )

    if not watch:
        collect_and_load()
        return 0

    interval = _positive_seconds(os.environ.get("PDP_COLLECTOR_POLL_SECONDS", "1800"))
    try:
        while True:
            try:
                collect_and_load()
            except Exception as error:
                print(
                    f"Screen Time collection failed ({type(error).__name__}): {error}",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def warehouse_config() -> WarehouseConfig:
    database = os.environ.get("MOTHERDUCK_DATABASE", "").strip()
    if not database:
        raise ConfigurationError("MOTHERDUCK_DATABASE is required")
    token = _env_or_keychain(
        os.environ, "PDP_SCREEN_TIME_MOTHERDUCK_TOKEN", "screen-time-motherduck-token"
    )
    return WarehouseConfig(database=database, token=token)


def load_pending(
    state: CollectorState,
    warehouse: Warehouse,
    *,
    now: datetime | None = None,
    publish_success: bool = True,
) -> None:
    """Confirm warehouse commits before local compaction or liveness publication."""
    completed_at = now or datetime.now(UTC)
    owner = str(uuid.uuid4())
    if not warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS):
        raise JobAlreadyRunning("loader already has an unexpired job lease")
    deadline = time.monotonic() + 20 * 60
    released = False
    try:
        health_details = []
        for source in get_sources("screen_time", all_streams=True):
            repository = ScreenTimeLocalRepository(state=state, source=source)
            pending_keys = {p.identity.object_key for p in state.pending(stream=source.stream)}
            refs = tuple(r for r in list_source_raw(repository, source) if r.key in pending_keys)
            if refs:
                summary = run_loader_objects(
                    repository,
                    warehouse,
                    refs,
                    source=source,
                    _lease_owner=owner,
                    _deadline=deadline,
                )
                for key in warehouse.succeeded_keys_for(refs, parser_version=source.parser_version):
                    state.mark_loaded(key, completed_at)
                if not summary.ok:
                    raise CollectorSourceError(f"{source.stream}: Raw ingestion failed")
            live_raw = tuple(list_source_raw(repository, source))
            health = source.audit(repository, live_raw, completed_at)
            if not health.ok or state.pending(stream=source.stream):
                raise CollectorSourceError(f"{source.stream}: collector audit failed")
            prune_receipts(warehouse, live_raw, parser_version=source.parser_version)
            health_details.append((source, health.details))
        if publish_success:
            finish_collection(
                warehouse,
                owner,
                heartbeats=[
                    (
                        source.monitor_name,
                        {
                            **details,
                            "source_id": source.source_id,
                            "stream": source.stream,
                            "scan_completed_at": completed_at.isoformat(),
                        },
                    )
                    for source, details in health_details
                ],
            )
            released = True
        else:
            warehouse.require_job_lock(owner)
        with state._connect() as connection:
            if connection.execute("PRAGMA freelist_count").fetchone()[0]:
                connection.execute("VACUUM")
    finally:
        if not released and warehouse.connection_usable:
            failure = sys.exception()
            try:
                warehouse.release_job_lock("loader", owner)
            except Exception:
                if failure is None:
                    raise
                failure.add_note("loader lease cleanup also failed")


def _print_collection_stats(stats: CollectionStats) -> None:
    print(
        json.dumps(
            {
                "devices": stats.devices,
                "segments": stats.segments,
                "archived": stats.archived,
                "skipped": stats.skipped,
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


def iphone_source(config: CollectorConfig) -> BiomeScreenTimeSource:
    return BiomeScreenTimeSource(
        sync_db_path=config.sync_db_path,
        remote_dir=config.app_in_focus_remote_dir,
    )


def mac_source(config: CollectorConfig) -> BiomeMacAppUsageSource:
    if config.mac_app_usage_local_dir is None:
        raise ConfigurationError("Mac AppUsage directory is not configured")
    return BiomeMacAppUsageSource(
        sync_db_path=config.sync_db_path,
        local_dir=config.mac_app_usage_local_dir,
    )


def collect_all(
    collectors: Sequence[ScreenTimeCollector],
    *,
    inactive_streams: Sequence[str] = (),
    inactive_state: CollectorState | None = None,
    clock: Callable[[], datetime] | None = None,
) -> CollectionStats:
    """Attempt each configured stream and report failure after all attempts."""
    results: list[CollectionStats] = []
    failures: list[Exception] = []
    for collector in collectors:
        try:
            results.append(collector.collect_once())
        except Exception as error:
            failures.append(RuntimeError(f"{collector.stream}: {error}"))
    if inactive_streams and inactive_state is None:
        raise ValueError("inactive streams require state")
    now = clock or (lambda: datetime.now(UTC))
    for stream in inactive_streams:
        assert inactive_state is not None
        try:
            inactive_state.record_successful_scan(CollectorScan(now(), (), {}), stream=stream)
        except Exception as error:
            failures.append(RuntimeError(f"{stream} deactivation: {error}"))
    if failures:
        raise ExceptionGroup("Screen Time collection failed", failures)
    return CollectionStats(
        devices=sum(result.devices for result in results),
        segments=sum(result.segments for result in results),
        archived=sum(result.archived for result in results),
        skipped=sum(result.skipped for result in results),
        deferred=sum(result.deferred for result in results),
    )

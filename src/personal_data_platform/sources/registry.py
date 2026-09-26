"""Explicit composition of the source and stream adapters available at runtime."""

from __future__ import annotations

from collections.abc import Callable

from .contracts import SourceAdapter
from .screen_time.adapter import MacAppUsageSource, ScreenTimeSource


def _screen_time_source() -> SourceAdapter:
    known_streams = tuple(
        stream for source_id, stream in _SOURCE_FACTORIES if source_id == "screen_time"
    )
    return ScreenTimeSource(known_streams=known_streams)


def _mac_app_usage_source() -> SourceAdapter:
    known_streams = tuple(
        stream for source_id, stream in _SOURCE_FACTORIES if source_id == "screen_time"
    )
    return MacAppUsageSource(known_streams=known_streams)


_SOURCE_FACTORIES: dict[tuple[str, str], Callable[[], SourceAdapter]] = {
    ("screen_time", "app-in-focus"): _screen_time_source,
    ("screen_time", "app-usage"): _mac_app_usage_source,
}
_DEFAULT_SOURCE = ("screen_time", "app-in-focus")


def source_ids() -> tuple[str, ...]:
    """Return the registered sources without creating runtime dependencies."""
    return tuple(sorted({source_id for source_id, _ in _SOURCE_FACTORIES}))


def get_source(source_id: str | None = None, stream: str | None = None) -> SourceAdapter:
    """Resolve one execution scope, requiring a stream when a source has several."""
    if source_id is None:
        if stream is not None:
            raise ValueError("stream requires source_id")
        return _SOURCE_FACTORIES[_DEFAULT_SOURCE]()
    candidates = {key: factory for key, factory in _SOURCE_FACTORIES.items() if key[0] == source_id}
    if not candidates:
        raise ValueError(f"unsupported source: {source_id}")
    if stream is None:
        if len(candidates) != 1:
            raise ValueError(f"source {source_id!r} has multiple streams; specify stream")
        return next(iter(candidates.values()))()
    factory = candidates.get((source_id, stream))
    if factory is None:
        raise ValueError(f"unsupported stream for source {source_id!r}: {stream}")
    return factory()


def get_sources(
    source_id: str | None = None, stream: str | None = None, *, all_streams: bool = False
) -> tuple[SourceAdapter, ...]:
    """Resolve one stream or an explicit source-wide execution scope."""
    if not all_streams:
        return (get_source(source_id, stream),)
    if source_id is None:
        raise ValueError("--all-streams requires source_id")
    if stream is not None:
        raise ValueError("--all-streams cannot be combined with --stream")
    keys = sorted(key for key in _SOURCE_FACTORIES if key[0] == source_id)
    if not keys:
        raise ValueError(f"unsupported source: {source_id}")
    return tuple(_SOURCE_FACTORIES[key]() for key in keys)

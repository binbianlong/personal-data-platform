"""Boundaries between ingestion infrastructure and source-specific behavior."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from personal_data_platform.raw.models import RawObject


class RawRepository(Protocol):
    def list_raw(self, prefix: str) -> Iterable[RawObject]: ...

    def get_raw(self, key: str, *, generation: int) -> bytes: ...


class RawCodec(Protocol):
    source_id: str
    stream: str
    schema_versions: tuple[int, ...]
    raw_prefixes: tuple[str, ...]
    raw_suffixes: tuple[str, ...]

    def validate_raw_key(self, key: str) -> None: ...

    def parse_raw_key(
        self, key: str, *, storage_created_at: datetime, storage_generation: int
    ) -> RawObject: ...


class DecodedBatch(Protocol):
    """Source rows written inside the warehouse-owned object transaction."""

    @property
    def parser_version(self) -> str: ...

    @property
    def record_count(self) -> int: ...

    def write(
        self, connection: Any, raw: RawObject, *, byte_size: int, loaded_at: datetime
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SourceHealth:
    ok: bool
    details: dict[str, Any]


class SourceAdapter(RawCodec, Protocol):
    required_relations: tuple[str, ...]
    retention_days: int
    lifecycle_grace_days: int
    dbt_selector: str
    monitor_name: str

    def decode(self, raw: RawObject, payload: bytes) -> DecodedBatch: ...

    def repository_from_env(self) -> RawRepository: ...

    def audit(
        self, repository: RawRepository, observations: Sequence[RawObject], now: datetime
    ) -> SourceHealth: ...

    def inventory(self, observations: Sequence[RawObject]) -> dict[str, object]: ...

    def legacy_scope(self, raw: RawObject) -> tuple[str, str] | None:
        """Supply old ingestion columns during the additive schema transition."""
        ...


def selected_prefixes(source: RawCodec, prefix: str | None = None) -> tuple[str, ...]:
    """Resolve a complete source inventory or an explicitly narrowed prefix."""
    prefixes = source.raw_prefixes
    if not prefixes or any(not value.endswith("/") for value in prefixes):
        raise ValueError("source Raw prefixes must be non-empty directories")
    if any(
        left.startswith(right)
        for index, left in enumerate(prefixes)
        for other, right in enumerate(prefixes)
        if index != other
    ):
        raise ValueError("source Raw prefixes must not overlap")
    if prefix is None:
        return prefixes
    if not prefix.endswith("/") or not any(prefix.startswith(value) for value in prefixes):
        raise ValueError("Raw prefix must be a directory inside the selected source namespace")
    return (prefix,)


def validate_observations(source: RawCodec, observations: Iterable[RawObject]) -> list[RawObject]:
    """Reject mixed scopes and metadata that disagrees with its immutable key."""
    materialized = list(observations)
    if len({value.key for value in materialized}) != len(materialized):
        raise RuntimeError("raw object listing returned duplicate keys")
    for raw in materialized:
        if (
            raw.source_id != source.source_id
            or raw.stream != source.stream
            or raw.schema_version not in source.schema_versions
        ):
            raise ValueError(f"Raw object does not belong to the selected source/stream: {raw.key}")
        source.validate_raw_key(raw.key)
        parsed = source.parse_raw_key(
            raw.key,
            storage_created_at=raw.storage_created_at,
            storage_generation=raw.storage_generation,
        )
        if parsed != raw:
            raise ValueError(f"Raw object metadata does not match its key: {raw.key}")
    return sorted(materialized, key=lambda value: (value.observed_at, value.key))


def list_source_raw(
    repository: RawRepository, source: RawCodec, prefix: str | None = None
) -> list[RawObject]:
    observations: list[RawObject] = []
    for selected in selected_prefixes(source, prefix):
        values = list(repository.list_raw(selected))
        if any(not value.key.startswith(selected) for value in values):
            raise ValueError("Raw listing returned an object outside the requested prefix")
        observations.extend(values)
    return validate_observations(source, observations)


def validate_runtime_policy(source: SourceAdapter) -> None:
    """Stop before cloud access if deployment and source retention contracts drift."""
    for name, expected in (
        ("PDP_RAW_RETENTION_DAYS", source.retention_days),
        ("PDP_LIFECYCLE_GRACE_DAYS", source.lifecycle_grace_days),
    ):
        value = os.environ.get(name)
        if value is not None and value != str(expected):
            raise ValueError(
                f"{name} does not match {source.source_id}/{source.stream}: {expected}"
            )
    for name, expected in (
        ("PDP_RAW_PREFIXES_JSON", source.raw_prefixes),
        ("PDP_RAW_SUFFIXES_JSON", source.raw_suffixes),
    ):
        value = os.environ.get(name)
        if value is None:
            continue
        try:
            configured = json.loads(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be a JSON array") from error
        if (
            not isinstance(configured, list)
            or not all(isinstance(item, str) for item in configured)
            or sorted(configured) != sorted(expected)
        ):
            raise ValueError(f"{name} does not match {source.source_id}/{source.stream}")

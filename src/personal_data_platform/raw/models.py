"""Source-independent identity of an immutable Raw observation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RawObject:
    """A source scope observed once, tied to an exact storage generation."""

    key: str
    source_id: str
    schema_version: int
    subject_key: str
    stream: str
    logical_key: str
    observed_at: datetime
    sha256: str
    storage_created_at: datetime
    storage_generation: int

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*", self.source_id) is None:
            raise ValueError("source_id must be a lowercase source name")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        if not all((self.key, self.subject_key, self.stream, self.logical_key)):
            raise ValueError("Raw identity and scope must not be empty")
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        for name in ("observed_at", "storage_created_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.storage_generation < 1:
            raise ValueError("storage_generation must be positive")

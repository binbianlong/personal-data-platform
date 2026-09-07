"""Source-independent result types for Raw loading."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LoadSummary:
    """Outcome of a loader run."""

    discovered: int
    skipped: int
    succeeded: int
    failed: int
    records: int

    @property
    def ok(self) -> bool:
        return self.failed == 0


class RawDecodeError(ValueError):
    """Raised when a Raw observation cannot be decoded atomically."""

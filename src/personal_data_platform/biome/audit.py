"""Local pending-delivery diagnostics; collector activity is recorded in run logs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository, SourceHealth

from .storage import BiomeLocalRepository


def audit_source(
    repository: RawRepository, observations: Sequence[RawObject], now: datetime
) -> SourceHealth:
    if not isinstance(repository, BiomeLocalRepository):
        return SourceHealth(
            False, {"local_state_unavailable": True, "collector_liveness": "local_logs"}
        )
    pending = len(repository.state.pending())
    return SourceHealth(
        pending == 0, {"pending_snapshot_count": pending, "collector_liveness": "local_logs"}
    )

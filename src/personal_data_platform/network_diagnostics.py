"""Opt-in, exclusive wall-time attribution of container network counters.

These are all non-loopback interface bytes, not destination-classified billing
bytes. Background traffic can fall into the currently executing phase.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_data_platform.logging_config import configure_logging

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Counters:
    sent: int
    received: int
    interfaces: tuple[str, ...]
    interface_counters: tuple[tuple[str, int, int], ...] = ()


def read_counters(path: Path = Path("/proc/net/dev")) -> Counters:
    sent = received = 0
    interfaces = []
    for line in path.read_text().splitlines()[2:]:
        name, values = line.split(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        fields = values.split()
        rx, tx = int(fields[0]), int(fields[8])
        if min(rx, tx) < 0:
            raise ValueError("negative network counter")
        interfaces.append((name, tx, rx))
        sent += tx
        received += rx
    if not interfaces:
        raise ValueError("no non-loopback network interface")
    counters = tuple(sorted(interfaces))
    return Counters(sent, received, tuple(name for name, _, _ in counters), counters)


@dataclass
class _Phase:
    sent: int = 0
    received: int = 0
    seconds: float = 0
    calls: int = 0


class NetworkProfile:
    def __init__(self, workload: str, reader: Callable[[], Counters]) -> None:
        self.workload = workload
        self.profile_id = uuid.uuid4().hex
        self.correlation_id: str | None = None
        self.reader = reader
        self.valid = True
        self.invalid_reason: str | None = None
        self._stack: list[str] = []
        self._phases: dict[str, _Phase] = {}
        self._last: Counters | None = None
        self._started = self._time = time.monotonic()
        self._finished = self._started
        self._advance()

    def _advance(self) -> None:
        now = time.monotonic()
        row = self._phases.setdefault("/".join(self._stack) or "unattributed", _Phase())
        row.seconds += max(0, now - self._time)
        self._time = now
        if not self.valid:
            return
        try:
            current = self.reader()
            if self._last is not None:
                if current.interfaces != self._last.interfaces:
                    self.valid = False
                    self.invalid_reason = "interfaces_changed"
                elif current.sent < self._last.sent or current.received < self._last.received:
                    self.valid = False
                    self.invalid_reason = "counter_reset"
                elif any(
                    sent < previous_sent or received < previous_received
                    for (_, sent, received), (_, previous_sent, previous_received) in zip(
                        current.interface_counters, self._last.interface_counters, strict=True
                    )
                ):
                    self.valid = False
                    self.invalid_reason = "counter_reset"
                else:
                    row.sent += current.sent - self._last.sent
                    row.received += current.received - self._last.received
            self._last = current
        except Exception:
            # Observability must not turn a successful acquisition into a retry.
            self.valid = False
            self.invalid_reason = "counter_unavailable"

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        self._advance()
        self._stack.append(name)
        key = "/".join(self._stack)
        self._phases.setdefault(key, _Phase()).calls += 1
        try:
            yield
        finally:
            self._advance()
            self._stack.pop()

    def finish(self) -> None:
        self._advance()
        self._finished = time.monotonic()

    def summary(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "correlation_id": self.correlation_id,
            "workload": self.workload,
            "counter_source": "proc_net_dev",
            "traffic_scope": "all_non_loopback",
            "attribution": "exclusive_wall_time",
            "valid": self.valid,
            "invalid_reason": self.invalid_reason,
            "interfaces": list(self._last.interfaces) if self._last else [],
            "sent_bytes": sum(row.sent for row in self._phases.values()) if self.valid else None,
            "received_bytes": sum(row.received for row in self._phases.values())
            if self.valid
            else None,
            "elapsed_seconds": round(self._finished - self._started, 6),
            "phases": [
                {
                    "phase": name,
                    "sent_bytes": row.sent if self.valid else None,
                    "received_bytes": row.received if self.valid else None,
                    "elapsed_seconds": round(row.seconds, 6),
                    "calls": row.calls,
                }
                for name, row in sorted(self._phases.items())
            ],
        }


_ACTIVE: ContextVar[NetworkProfile | None] = ContextVar("network_profile", default=None)


@contextmanager
def network_run(workload: str) -> Iterator[NetworkProfile | None]:
    existing = _ACTIVE.get()
    if existing is not None:
        yield existing
        return
    if os.environ.get("PDP_NETWORK_DIAGNOSTICS", "").lower() != "true":
        yield None
        return
    profile = NetworkProfile(workload, read_counters)
    token = _ACTIVE.set(profile)
    try:
        yield profile
    finally:
        _ACTIVE.reset(token)
        profile.finish()
        try:
            configure_logging(__name__)
            LOGGER.info(
                "container network profile",
                extra={"event": "network_profile", "summary": profile.summary()},
            )
        except Exception:
            pass


@contextmanager
def network_phase(name: str) -> Iterator[None]:
    profile = _ACTIVE.get()
    if profile is None:
        yield
    else:
        with profile.phase(name):
            yield


def _sql_phase(query: object) -> str:
    if not isinstance(query, str):
        return "other_sql"
    sql = query.lstrip().upper()
    for prefix, phase in (
        ("BEGIN", "transaction_begin"),
        ("COMMIT", "transaction_commit"),
        ("ROLLBACK", "transaction_rollback"),
    ):
        if sql.startswith(prefix):
            return phase
    read = sql.startswith(("SELECT", "WITH", "EXPLAIN"))
    target = re.search(r"\b(?:FROM|INTO|UPDATE)\s+([A-Z_][A-Z0-9_.]*)", sql)
    table = target[1] if target else ""
    if table == "OPS.JOB_LOCK":
        return "lock_check" if read else "lock_write"
    if table.startswith("INFORMATION_SCHEMA.") or table == "OPS.SCHEMA_MIGRATION":
        return "schema_check"
    if table == "OPS.FITBIT_COVERAGE":
        return "coverage_read" if read else "coverage_write"
    if table == "OPS.FITBIT_DELETED_RECORD":
        return "deletion_read" if read else "deletion_write"
    if table.startswith("BASE."):
        return "data_compare" if read else "data_write"
    if table.startswith("OPS."):
        return "metadata_read" if read else "metadata_write"
    return "other_sql"


class _MeasuredConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._result_phase = "other_sql"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        self._result_phase = _sql_phase(query)
        with network_phase(self._result_phase):
            result = self._connection.execute(query, *args, **kwargs)
        return self if result is self._connection else result

    def fetchall(self) -> Any:
        with network_phase(self._result_phase):
            return self._connection.fetchall()

    def fetchone(self) -> Any:
        with network_phase(self._result_phase):
            return self._connection.fetchone()

    def close(self) -> None:
        with network_phase("db_close"):
            self._connection.close()


def instrument_connection(connection: Any) -> Any:
    if _ACTIVE.get() is None or isinstance(connection, _MeasuredConnection):
        return connection
    return _MeasuredConnection(connection)

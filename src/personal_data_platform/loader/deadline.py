"""Bound active database work without allowing a writer to outlive its lease."""

from __future__ import annotations

from threading import Timer

from personal_data_platform.storage.motherduck import Warehouse


def interrupt_after(warehouse: Warehouse, seconds: float) -> Timer:
    if seconds <= 0:
        raise ValueError("warehouse deadline must be positive")

    def interrupt() -> None:
        if warehouse.connection_usable:
            warehouse.connection.interrupt()

    timer = Timer(seconds, interrupt)
    timer.daemon = True
    timer.start()
    return timer

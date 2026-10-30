"""Compose the configured warehouse implementation at runtime entrypoints."""

from __future__ import annotations

from . import motherduck
from .motherduck import WarehouseConfig as WarehouseConfig
from .warehouse import Warehouse


def open_warehouse(config: WarehouseConfig | None = None) -> Warehouse:
    settings = config if config is not None else WarehouseConfig.from_env()
    return motherduck.Warehouse(motherduck.connect(settings))

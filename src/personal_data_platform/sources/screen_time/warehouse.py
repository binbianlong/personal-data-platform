"""Screen Time receipt retention in the warehouse."""

from __future__ import annotations

from collections.abc import Iterable

from personal_data_platform.raw.models import RawObject
from personal_data_platform.storage.warehouse import Warehouse


def prune_receipts(
    warehouse: Warehouse, live_raw: Iterable[RawObject], *, parser_version: str
) -> None:
    """Remove superseded successes only where one live Raw is warehouse-confirmed."""

    warehouse.prune_screen_time_receipts(live_raw, parser_version=parser_version)

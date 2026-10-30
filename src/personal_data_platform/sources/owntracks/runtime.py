"""Compose OwnTracks receivers and the common object loader."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import uvicorn

from personal_data_platform.config import secret_config
from personal_data_platform.loader.job import run_loader_objects
from personal_data_platform.logging_config import configure_logging
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository
from personal_data_platform.storage.factory import open_warehouse
from personal_data_platform.storage.gcs import GCSRawRepository
from personal_data_platform.storage.warehouse import Warehouse

from .adapter import OwnTracksSource
from .service import OwnTracksConfig, create_receiver_app, create_worker_app


@dataclass
class OwnTracksProcessor:
    repository: RawRepository
    warehouse_factory: Callable[[], Warehouse]
    paused: bool = False

    def process(self, raw: RawObject) -> bool:
        if self.paused:
            return False
        warehouse = self.warehouse_factory()
        try:
            warehouse.migrate()
            return run_loader_objects(
                self.repository,
                warehouse,
                [raw],
                source=OwnTracksSource(),
                _deadline=time.monotonic() + 480,
            ).ok
        finally:
            warehouse.close()


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def run_server(*, worker: bool) -> int:
    configure_logging("personal_data_platform.sources.owntracks")
    source = OwnTracksSource()
    repository = GCSRawRepository.from_env(source=source)
    if worker:
        paused = os.environ.get("PDP_OWNTRACKS_PROCESSING_PAUSED", "false")
        if paused not in ("true", "false"):
            raise ValueError("PDP_OWNTRACKS_PROCESSING_PAUSED must be true or false")
        app = create_worker_app(
            processor=OwnTracksProcessor(
                repository=repository,
                warehouse_factory=lambda: open_warehouse(),
                paused=paused == "true",
            ),
            subscription=_required("PDP_OWNTRACKS_PUBSUB_SUBSCRIPTION"),
            bucket=_required("GCS_BUCKET"),
        )
    else:
        config = secret_config(
            "PDP_OWNTRACKS_CONFIG", ("username", "password", "device_id", "device_key")
        )
        app = create_receiver_app(config=OwnTracksConfig(**config), repository=repository)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
        access_log=False,
    )
    return 0

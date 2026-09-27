"""Durable delivery, one-window workers, and concurrent HTTP entrypoints."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from personal_data_platform.loader.job import (
    LOADER_LEASE_SECONDS,
    JobAlreadyRunning,
    run_loader_objects,
)
from personal_data_platform.sources.contracts import RawRepository
from personal_data_platform.storage.motherduck import Warehouse

from .adapter import FitbitSource
from .models import Snapshot, Window, object_dict, string
from .raw import SnapshotRepository, save_snapshot
from .receipts import Receipt, ReceiptRepository, validate_receipt_key
from .signatures import GoogleTaskIdentity
from .webhook import AuthenticationError, GoogleHealthAuthenticator, PayloadError, Verification
from .writer import expand_window

LOGGER = logging.getLogger(__name__)


class ProcessingPaused(RuntimeError):
    """Receipts remain durable while the operating budget gate is closed."""


class Queue(Protocol):
    def enqueue(self, receipt_key: str) -> None: ...


class SnapshotStore(RawRepository, SnapshotRepository, Protocol):
    pass


class Client(Protocol):
    def fetch(self, window: Window, *, subject_key: str) -> Snapshot: ...


class Worker(Protocol):
    def run(self, receipt_key: str) -> bool: ...


class ReceiptWorker:
    def __init__(
        self,
        *,
        receipts: ReceiptRepository,
        repository: SnapshotStore,
        client: Client,
        warehouse_factory: Callable[[], Warehouse],
        subject_key: str,
        paused: bool = False,
    ) -> None:
        self.receipts, self.repository, self.client = receipts, repository, client
        self._warehouse = warehouse_factory
        self.subject_key = subject_key
        self.paused = paused

    def run(self, receipt_key: str) -> bool:
        """Process one day/type; continuation tasks keep requests bounded."""
        if self.paused:
            raise ProcessingPaused("Fitbit processing is paused")
        stored = self.receipts.read(receipt_key)
        if stored.receipt.subject_key != self.subject_key:
            raise ValueError("receipt subject does not match runtime")
        if stored.receipt.completed_at is not None:
            return True
        pending = [index for index, item in enumerate(stored.receipt.work) if not item.completed]
        if not pending:
            raise ValueError("receipt has inconsistent completion state")
        index = pending[0]
        item = stored.receipt.work[index]
        warehouse = self._warehouse()
        owner = str(uuid.uuid4())
        acquired = False
        try:
            acquired = warehouse.acquire_job_lock(
                "loader", owner, lease_seconds=LOADER_LEASE_SECONDS
            )
            if not acquired:
                raise JobAlreadyRunning("shared loader lease is busy")
            if item.raw is None:
                window = expand_window(warehouse, self.subject_key, item.window)
                snapshot = self.client.fetch(window, subject_key=self.subject_key)
                raw = save_snapshot(self.repository, snapshot)
                item = replace(item, window=window, raw=raw)
                work = list(stored.receipt.work)
                work[index] = item
                stored = self.receipts.replace(stored, replace(stored.receipt, work=tuple(work)))
            assert item.raw is not None
            summary = run_loader_objects(
                self.repository, warehouse, [item.raw], source=FitbitSource(), _lease_owner=owner
            )
            if not summary.ok:
                raise RuntimeError("Fitbit Raw load failed")
            work = list(stored.receipt.work)
            work[index] = replace(item, completed=True)
            complete = all(item.completed for item in work)
            receipt = replace(
                stored.receipt,
                work=tuple(work),
                completed_at=datetime.now(UTC) if complete else None,
            )
            self.receipts.replace(stored, receipt)
            return complete
        finally:
            if acquired and warehouse.connection_usable:
                warehouse.release_job_lock("loader", owner)
            warehouse.close()


async def _body(request: Request) -> bytes:
    content = bytearray()
    async for part in request.stream():
        content.extend(part)
        if len(content) > 1024 * 1024:
            raise PayloadError("request exceeds one MiB")
    return bytes(content)


def create_app(
    *,
    authenticator: GoogleHealthAuthenticator,
    identity: GoogleTaskIdentity,
    receipts: ReceiptRepository,
    queue: Queue,
    worker: Worker,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def health() -> Response:
        return Response(status_code=200)

    @app.post("/webhooks/fitbit")
    async def webhook(request: Request) -> Response:
        try:
            payload = await run_in_threadpool(
                authenticator.authenticate,
                authorization=request.headers.get("authorization"),
                signature_header=request.headers.get("google-health-api-signature"),
                content_type=request.headers.get("content-type"),
                body=await _body(request),
            )
            if isinstance(payload, Verification):
                return Response(status_code=200)
            receipt = Receipt.create(
                payload.subject_key, payload.windows, received_at=datetime.now(UTC)
            )
            stored = await run_in_threadpool(receipts.create, receipt)
            await run_in_threadpool(queue.enqueue, stored.receipt.key)
            return Response(status_code=204)
        except AuthenticationError:
            return Response(status_code=401)
        except PayloadError:
            return Response(status_code=400)
        except Exception as error:
            LOGGER.error("fitbit webhook failed: %s", type(error).__name__)
            return Response(status_code=503)

    @app.post("/internal/tasks/fitbit")
    async def task(request: Request) -> Response:
        try:
            await run_in_threadpool(identity.authenticate, request.headers.get("authorization"))
        except AuthenticationError:
            return Response(status_code=401)
        try:
            payload = object_dict(json.loads(await _body(request)))
            key = string(payload["receipt_key"])
            validate_receipt_key(key)
        except (ValueError, KeyError, UnicodeError):
            return Response(status_code=400)
        try:
            complete = await run_in_threadpool(worker.run, key)
            if not complete:
                await run_in_threadpool(queue.enqueue, key)
            return Response(status_code=204)
        except ProcessingPaused:
            # The durable receipt stays pending for repair after processing resumes.
            LOGGER.info("fitbit task deferred while processing is paused")
            return Response(status_code=204)
        except Exception as error:
            LOGGER.error("fitbit task failed: %s", type(error).__name__)
            return Response(status_code=503)

    return app

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
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository
from personal_data_platform.storage.motherduck import Warehouse

from .adapter import FitbitSource
from .models import Notification, Snapshot, Window, object_dict, string
from .raw import SnapshotRepository, encode_snapshot
from .receipts import (
    Receipt,
    ReceiptReadConflict,
    ReceiptRepository,
    ReceiptWork,
    StoredReceipt,
    validate_receipt_key,
)
from .signatures import GoogleTaskIdentity
from .webhook import (
    AuthenticationError,
    GoogleHealthAuthenticator,
    PayloadError,
    Verification,
    split_notifications,
)
from .writer import can_skip_snapshot, expand_window

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
        warehouse = self._warehouse()
        owner = str(uuid.uuid4())
        acquired = False
        try:
            acquired = warehouse.acquire_job_lock(
                "loader", owner, lease_seconds=LOADER_LEASE_SECONDS
            )
            if not acquired:
                raise JobAlreadyRunning("shared loader lease is busy")
            # A duplicate Cloud Task can race with a completed task before the lease.
            stored = self.receipts.read(receipt_key)
            if stored.receipt.subject_key != self.subject_key:
                raise ValueError("receipt subject does not match runtime")
            if stored.receipt.completed_at is not None:
                return True
            pending = [
                index for index, item in enumerate(stored.receipt.work) if not item.completed
            ]
            if not pending:
                raise ValueError("receipt has inconsistent completion state")
            index = pending[0]
            item = stored.receipt.work[index]
            intents = self._intents(warehouse, receipt_key, index)
            if item.raw is not None:
                if item.raw.key in warehouse.succeeded_keys_for(
                    (item.raw,), parser_version=FitbitSource.parser_version
                ):
                    self._load(warehouse, owner, (item.raw,))
                    return self._complete(stored, index, item)
                existing = self.repository.head_raw(item.raw.key)
                if (
                    existing is not None
                    and existing.storage_generation == item.raw.storage_generation
                ):
                    self._load(warehouse, owner, (item.raw,))
                    return self._complete(stored, index, item)
                window = Window(
                    item.window.data_type,
                    min([item.window.start, *(row[1] for row in intents)]),
                    max([item.window.end, *(row[2] for row in intents)]),
                )
                minimum_fetched_at = max([item.raw.observed_at, *(row[3] for row in intents)])
            elif intents:
                located = [(row, self.repository.head_raw(row[0])) for row in intents]
                if all(raw is not None for _, raw in located):
                    refs = tuple(raw for _, raw in located if raw is not None)
                    self._load(warehouse, owner, refs)
                    newest = located[-1]
                    assert newest[1] is not None
                    return self._complete(
                        stored,
                        index,
                        replace(
                            item,
                            window=Window(item.window.data_type, newest[0][1], newest[0][2]),
                            raw=newest[1],
                        ),
                    )
                window = Window(
                    item.window.data_type,
                    min(row[1] for row in intents),
                    max(row[2] for row in intents),
                )
                minimum_fetched_at = max(row[3] for row in intents)
            else:
                window = expand_window(warehouse, self.subject_key, item.window)
                minimum_fetched_at = None
            snapshot = self.client.fetch(window, subject_key=self.subject_key)
            if snapshot.subject_key != self.subject_key or snapshot.window != window:
                raise ValueError("Fitbit client returned a different acquisition scope")
            if minimum_fetched_at is not None and snapshot.fetched_at <= minimum_fetched_at:
                raise RuntimeError("recovery acquisition must be newer than its unresolved Raw")
            if item.raw is None and not intents and can_skip_snapshot(warehouse, snapshot):
                result = self._complete(
                    stored,
                    index,
                    replace(
                        item,
                        window=window,
                        completed=True,
                        fetched_at=snapshot.fetched_at,
                        source_sha256=snapshot.source_sha256(),
                    ),
                )
                LOGGER.info(
                    "fitbit acquisition fetched=1 raw_saved=0 raw_skipped=1 compressed_bytes=0"
                )
                return result
            raw = self._save(warehouse, receipt_key, index, snapshot)
            item = replace(item, window=window, raw=raw)
            work = list(stored.receipt.work)
            work[index] = item
            stored = self.receipts.replace(stored, replace(stored.receipt, work=tuple(work)))
            self._load(warehouse, owner, (raw,))
            return self._complete(stored, index, item)
        finally:
            if acquired and warehouse.connection_usable:
                warehouse.release_job_lock("loader", owner)
            warehouse.close()

    @staticmethod
    def _intents(
        warehouse: Warehouse, receipt_key: str, index: int
    ) -> list[tuple[str, datetime, datetime, datetime]]:
        return [
            (str(key), start, end, fetched)
            for key, start, end, fetched in warehouse.query_rows(
                "SELECT raw_key, range_start, range_end, fetched_at "
                "FROM ops.fitbit_raw_intent WHERE receipt_key=? AND work_index=? "
                "ORDER BY fetched_at, raw_key",
                [receipt_key, index],
            )
        ]

    def _save(
        self, warehouse: Warehouse, receipt_key: str, index: int, snapshot: Snapshot
    ) -> RawObject:
        key, compressed = encode_snapshot(snapshot)
        window = snapshot.window
        warehouse.connection.execute(
            "INSERT INTO ops.fitbit_raw_intent VALUES (?,?,?,?,?,?,?,?)",
            [
                receipt_key,
                index,
                snapshot.subject_key,
                window.data_type,
                window.start,
                window.end,
                key,
                snapshot.fetched_at,
            ],
        )
        # Confirm the write-ahead barrier before an immutable GCS PUT.
        if (
            warehouse.query_value(
                "SELECT count(*) FROM ops.fitbit_raw_intent WHERE raw_key=?", [key]
            )
            != 1
        ):
            raise RuntimeError("Fitbit Raw intent was not persisted")
        raw = self.repository.put_raw_object(key, compressed)
        LOGGER.info(
            "fitbit acquisition fetched=1 raw_saved=1 raw_skipped=0 compressed_bytes=%d",
            len(compressed),
        )
        return raw

    def _load(self, warehouse: Warehouse, owner: str, refs: tuple[RawObject, ...]) -> None:
        summary = run_loader_objects(
            self.repository, warehouse, refs, source=FitbitSource(), _lease_owner=owner
        )
        if not summary.ok:
            raise RuntimeError("Fitbit Raw load failed")
        # A previous transaction may have loaded a Raw before its intent was
        # repaired. Only a verified success may retire that stale barrier.
        succeeded = warehouse.succeeded_keys_for(refs, parser_version=FitbitSource.parser_version)
        for raw in refs:
            if raw.key not in succeeded:
                raise RuntimeError("Fitbit Raw success state was not recorded")
            warehouse.connection.execute(
                "DELETE FROM ops.fitbit_raw_intent WHERE raw_key=?", [raw.key]
            )

    def _complete(self, stored: StoredReceipt, index: int, item: ReceiptWork) -> bool:
        work = list(stored.receipt.work)
        work[index] = replace(item, completed=True)
        complete = all(value.completed for value in work)
        receipt = replace(
            stored.receipt,
            work=tuple(work),
            completed_at=datetime.now(UTC) if complete else None,
        )
        self.receipts.replace(stored, receipt)
        return complete

    def recover_orphan_intents(self, *, limit: int = 1) -> int:
        """Recover abandoned Raw intents after their receipt has expired.

        Pending receipts are left to the normal Cloud Task path. A bounded
        repair keeps the scheduled reconciliation job within its lease.
        """
        if self.paused:
            raise ProcessingPaused("Fitbit processing is paused")
        if limit < 1:
            raise ValueError("recovery limit must be positive")
        warehouse = self._warehouse()
        owner = str(uuid.uuid4())
        acquired = False
        recovered = 0
        try:
            acquired = warehouse.acquire_job_lock(
                "loader", owner, lease_seconds=LOADER_LEASE_SECONDS
            )
            if not acquired:
                raise JobAlreadyRunning("shared loader lease is busy")
            groups = warehouse.query_rows(
                "SELECT receipt_key, work_index FROM ops.fitbit_raw_intent "
                "WHERE subject_key=? GROUP BY receipt_key, work_index "
                "ORDER BY min(fetched_at)",
                [self.subject_key],
            )
            for receipt_key, work_index in groups:
                if recovered >= limit:
                    break
                try:
                    receipt = self.receipts.read(receipt_key).receipt
                except FileNotFoundError:
                    receipt = None
                if receipt is not None and receipt.completed_at is None:
                    continue
                intents = self._intents(warehouse, receipt_key, work_index)
                if not intents:
                    continue
                located = [(row, self.repository.head_raw(row[0])) for row in intents]
                if all(raw is not None for _, raw in located):
                    self._load(
                        warehouse,
                        owner,
                        tuple(raw for _, raw in located if raw is not None),
                    )
                else:
                    types = warehouse.query_rows(
                        "SELECT DISTINCT data_type FROM ops.fitbit_raw_intent "
                        "WHERE receipt_key=? AND work_index=?",
                        [receipt_key, work_index],
                    )
                    if len(types) != 1:
                        raise RuntimeError("orphan Raw intent has inconsistent data types")
                    window = Window(
                        types[0][0],
                        min(row[1] for row in intents),
                        max(row[2] for row in intents),
                    )
                    snapshot = self.client.fetch(window, subject_key=self.subject_key)
                    if snapshot.subject_key != self.subject_key or snapshot.window != window:
                        raise ValueError("Fitbit client returned a different acquisition scope")
                    if snapshot.fetched_at <= max(row[3] for row in intents):
                        raise RuntimeError(
                            "recovery acquisition must be newer than its unresolved Raw"
                        )
                    raw = self._save(warehouse, receipt_key, work_index, snapshot)
                    self._load(warehouse, owner, (raw,))
                recovered += 1
            return recovered
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
        except (JobAlreadyRunning, ReceiptReadConflict) as error:
            LOGGER.info("fitbit task deferred: %s", type(error).__name__)
            return Response(status_code=503)
        except Exception as error:
            LOGGER.error("fitbit task failed: %s", type(error).__name__)
            return Response(status_code=503)

    return app


class NotificationPublisher(Protocol):
    def publish(self, notification: Notification) -> str: ...


def create_pubsub_app(
    *, authenticator: GoogleHealthAuthenticator, notifications: NotificationPublisher
) -> FastAPI:
    """Keep the receiver independent of warehouse, Raw storage and OAuth access."""
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
            received_at = datetime.now(UTC)
            work = []
            for windows in payload.groups or (payload.windows,):
                notification = Notification(
                    uuid.uuid4().hex, payload.subject_key, windows, received_at
                )
                work.extend(split_notifications(notification, max_units=1000 - len(work)))
            for notification in work:
                await run_in_threadpool(notifications.publish, notification)
            return Response(status_code=204)
        except AuthenticationError:
            return Response(status_code=401)
        except PayloadError:
            LOGGER.warning("fitbit webhook payload rejected; manual range sync may be required")
            return Response(status_code=400)
        except Exception as error:
            LOGGER.error("fitbit webhook publish failed: %s", type(error).__name__)
            return Response(status_code=503)

    return app

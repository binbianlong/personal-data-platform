"""Authenticate and publish bounded webhook notifications."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .acquisition import AcquisitionRunner
from .models import DATE_TYPES, Notification, Window, parse_time
from .notifications import decode_notification
from .webhook import (
    AuthenticationError,
    GoogleHealthAuthenticator,
    PayloadError,
    Verification,
    merge_windows,
    split_notifications,
)

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .batch import NotificationDrainer


def _queue_range(window: Window) -> dict[str, object]:
    return {
        "data_type": window.data_type,
        "time_basis": "civil" if window.data_type in DATE_TYPES else window.time_basis,
        "queue_start": window.start.isoformat(),
        "queue_end": window.end.isoformat(),
    }


def _coalesce_steps(
    work: list[tuple[Notification, str]],
) -> list[tuple[Notification, tuple[str, ...]]]:
    """Coalesce this delivery's steps, keeping each scope independently retryable."""
    steps = [
        (notice, source) for notice, source in work if notice.acquisition_mode == "bounded_steps"
    ]
    if len({source for _, source in steps}) < 2:
        return [(notice, (source,)) for notice, source in work]
    first = steps[0][0]
    merged = Notification(
        uuid.uuid4().hex,
        first.subject_key,
        merge_windows(tuple(notice.windows[0] for notice, _ in steps)),
        first.received_at,
        acquisition_mode="bounded_steps",
    )
    coalesced = []
    for unit in split_notifications(merged):
        window = unit.windows[0]
        coalesced.append(
            (
                unit,
                tuple(
                    dict.fromkeys(
                        source
                        for notice, source in steps
                        if notice.windows[0].time_basis == window.time_basis
                        and notice.windows[0].start < window.end
                        and window.start < notice.windows[0].end
                    )
                ),
            )
        )
    result = []
    for notice, source in work:
        if notice.acquisition_mode == "bounded_steps":
            result.extend(coalesced)
            coalesced = []
        else:
            result.append((notice, (source,)))
    return result


async def _body(request: Request) -> bytes:
    content = bytearray()
    async for part in request.stream():
        content.extend(part)
        if len(content) > 1024 * 1024:
            raise PayloadError("request exceeds one MiB")
    return bytes(content)


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
                return Response(status_code=201)
            received_at = datetime.now(UTC)
            delivery_id = uuid.uuid4().hex
            work: list[tuple[Notification, str]] = []
            source_ids: list[str] = []
            for index, windows in enumerate(payload.groups or (payload.windows,)):
                mode = payload.group_modes[index] if payload.group_modes else "legacy"
                if mode in ("bounded_minutes", "bounded_steps") and any(
                    interval.physical_start is not None
                    and parse_time(interval.physical_start) >= received_at
                    for interval in payload.observations[index].intervals
                ):
                    raise PayloadError("physical notification is entirely in the future")
                notification = Notification(
                    uuid.uuid4().hex,
                    payload.subject_key,
                    windows,
                    received_at,
                    acquisition_mode=mode,
                )
                source_ids.append(notification.notification_id)
                work.extend(
                    (unit, notification.notification_id)
                    for unit in split_notifications(notification, max_units=1000 - len(work))
                )
            queued_work = _coalesce_steps(work)
            for source_id, observation in zip(source_ids, payload.observations):
                digest = observation.sha256()
                for index, interval in enumerate(observation.intervals):
                    LOGGER.info(
                        "fitbit notification received",
                        extra={
                            "event": "fitbit_notification_received",
                            "status": "received",
                            "summary": {
                                "delivery_id": delivery_id,
                                "source_notification_id": source_id,
                                "notification_hash": digest,
                                "received_at": received_at.isoformat(),
                                "data_type": observation.data_type,
                                "operation": observation.operation,
                                "record_id": observation.record_id,
                                "interval_index": index,
                                "interval_count": len(observation.intervals),
                                **asdict(interval),
                            },
                        },
                    )
            for notification, source_ids_for_message in queued_work:
                summary = {
                    "delivery_id": delivery_id,
                    "source_notification_id": source_ids_for_message[0]
                    if len(source_ids_for_message) == 1
                    else None,
                    "source_notification_ids": list(source_ids_for_message),
                    "notification_id": notification.notification_id,
                    "pubsub_message_id": None,
                    "acquisition_mode": notification.acquisition_mode,
                    **_queue_range(notification.windows[0]),
                }
                try:
                    message_id = await run_in_threadpool(notifications.publish, notification)
                except Exception as error:
                    LOGGER.error(
                        "fitbit notification publish result unknown",
                        extra={
                            "event": "fitbit_notification_publish",
                            "status": "unknown",
                            "summary": {**summary, "error_type": type(error).__name__},
                        },
                    )
                    raise
                LOGGER.info(
                    "fitbit notification published",
                    extra={
                        "event": "fitbit_notification_publish",
                        "status": "succeeded",
                        "summary": {**summary, "pubsub_message_id": message_id},
                    },
                )
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


def create_worker_app(
    *, runner: AcquisitionRunner, subscription: str, drainer: NotificationDrainer | None = None
) -> FastAPI:
    """Cloud Run IAM authenticates the push caller before requests reach this app."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def health() -> Response:
        return Response(status_code=200)

    @app.post("/notifications/fitbit/drain")
    async def drain() -> Response:
        if drainer is None:
            return Response(status_code=503)
        result = await run_in_threadpool(drainer.run)
        return JSONResponse(asdict(result), status_code=200 if result.ok else 503)

    @app.post("/notifications/fitbit")
    async def ingest(request: Request) -> Response:
        try:
            envelope = json.loads(await _body(request))
            if not isinstance(envelope, dict) or envelope.get("subscription") != subscription:
                raise ValueError("unexpected push subscription")
            message = envelope["message"]
            encoded = message["data"]
            if not isinstance(encoded, str):
                raise ValueError("push data must be base64 text")
            notification = decode_notification(base64.b64decode(encoded, validate=True))
            if notification.subject_key != runner.subject_key:
                raise ValueError("unexpected notification subject")
        except (ValueError, KeyError, TypeError, binascii.Error, PayloadError):
            LOGGER.error("fitbit invalid push notification")
            return Response(status_code=400)
        started_at = datetime.now(UTC)
        message_id = message.get("messageId")
        attempt_id = uuid.uuid4().hex
        summary: dict[str, object] = {
            "notification_id": notification.notification_id,
            "pubsub_message_id": message_id if isinstance(message_id, str) else None,
            "attempt_id": attempt_id,
            "acquisition_mode": notification.acquisition_mode,
            "received_at": notification.received_at.isoformat(),
            "started_at": started_at.isoformat(),
            **_queue_range(notification.windows[0]),
        }
        try:
            result = await run_in_threadpool(
                runner.process_notification, notification, attempt_id=attempt_id
            )
            finished_at = datetime.now(UTC)
            summary.update(
                {
                    "finished_at": finished_at.isoformat(),
                    "completed_scopes": result.completed_scopes,
                    "failed_scopes": result.failed_scopes,
                    "deferred_scopes": result.deferred_scopes,
                }
            )
            if not result.ok:
                LOGGER.log(
                    logging.ERROR if result.failed_scopes else logging.INFO,
                    "fitbit push deferred or failed",
                    extra={"event": "fitbit_push", "status": "retry", "summary": summary},
                )
                return Response(status_code=503)
            committed_at = finished_at
            LOGGER.info(
                "fitbit push committed",
                extra={
                    "event": "fitbit_push",
                    "status": "succeeded",
                    "summary": {
                        **summary,
                        "committed_at": committed_at.isoformat(),
                        "lag_seconds": (committed_at - notification.received_at).total_seconds(),
                    },
                },
            )
            return Response(status_code=204)
        except Exception as error:
            LOGGER.error(
                "fitbit push failed error_type=%s",
                type(error).__name__,
                extra={
                    "event": "fitbit_push",
                    "status": "retry",
                    "summary": {
                        **summary,
                        "finished_at": datetime.now(UTC).isoformat(),
                        "completed_scopes": None,
                        "failed_scopes": None,
                        "deferred_scopes": None,
                        "error_type": type(error).__name__,
                    },
                },
            )
            return Response(status_code=503)

    return app

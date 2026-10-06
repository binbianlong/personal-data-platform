"""Authenticate and publish bounded webhook notifications."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from .models import Notification
from .webhook import (
    AuthenticationError,
    GoogleHealthAuthenticator,
    PayloadError,
    Verification,
    split_notifications,
)

LOGGER = logging.getLogger(__name__)


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
            work: list[Notification] = []
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

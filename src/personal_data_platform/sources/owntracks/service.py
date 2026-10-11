"""HTTP authentication and generation-pinned GCS notification delivery."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic
from starlette.concurrency import run_in_threadpool

from personal_data_platform.loader.job import JobAlreadyRunning
from personal_data_platform.raw.models import RawObject

from .adapter import OwnTracksSource
from .raw import prepare_location

LOGGER = logging.getLogger(__name__)


class RawWriter(Protocol):
    def put_compressed_raw(self, key: str, compressed_bytes: bytes) -> None: ...


class NotificationProcessor(Protocol):
    def process(self, raw: RawObject) -> bool: ...


@dataclass(frozen=True)
class OwnTracksConfig:
    username: str
    password: str
    device_id: str
    device_key: str

    def __post_init__(self) -> None:
        if not self.password or any(
            re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None
            for value in (self.username, self.device_id)
        ):
            raise ValueError("invalid OwnTracks identification configuration")
        if re.fullmatch(r"[0-9a-f]{64}", self.device_key) is None:
            raise ValueError("invalid OwnTracks pseudonymous device key")


class BodyTooLarge(ValueError):
    pass


async def _body(request: Request) -> bytes:
    content = bytearray()
    async for part in request.stream():
        if len(content) + len(part) > 1024 * 1024:
            raise BodyTooLarge("request exceeds one MiB")
        content.extend(part)
    return bytes(content)


def create_receiver_app(*, config: OwnTracksConfig, repository: RawWriter) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    security = HTTPBasic(auto_error=False)

    @app.get("/healthz")
    def health() -> Response:
        return Response(status_code=200)

    @app.post("/webhooks/owntracks")
    async def receive(request: Request) -> Response:
        credentials = await security(request)
        if credentials is None or not (
            hmac.compare_digest(credentials.username.encode(), config.username.encode())
            & hmac.compare_digest(credentials.password.encode(), config.password.encode())
        ):
            return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
        if any(
            request.headers.get(name, expected) != expected
            for name, expected in (("x-limit-u", config.username), ("x-limit-d", config.device_id))
        ):
            return Response(status_code=403)
        try:
            prepared = prepare_location(await _body(request), device_key=config.device_key)
            if prepared is not None:
                key, _, compressed = prepared
                await run_in_threadpool(repository.put_compressed_raw, key, compressed)
            return JSONResponse([])
        except BodyTooLarge:
            return Response(status_code=413)
        except ValueError:
            return Response(status_code=400)
        except Exception as error:
            LOGGER.error("OwnTracks Raw save failed (%s)", type(error).__name__)
            return Response(status_code=503)

    return app


def _notification(payload: bytes, *, subscription: str, bucket: str) -> RawObject:
    envelope = json.loads(payload)
    if not isinstance(envelope, dict) or envelope.get("subscription") != subscription:
        raise ValueError("unexpected OwnTracks subscription")
    message = envelope["message"]
    attributes = message["attributes"]
    data = json.loads(base64.b64decode(message["data"], validate=True))
    if (
        attributes["eventType"] != "OBJECT_FINALIZE"
        or attributes["payloadFormat"] != "JSON_API_V1"
        or attributes["bucketId"] != bucket
        or data["bucket"] != bucket
        or attributes["objectId"] != data["name"]
        or attributes["objectGeneration"] != data["generation"]
    ):
        raise ValueError("unexpected OwnTracks GCS notification")
    generation = int(data["generation"])
    if generation < 1 or str(generation) != data["generation"]:
        raise ValueError("invalid GCS generation")
    created = datetime.fromisoformat(data["timeCreated"])
    if created.tzinfo is None:
        raise ValueError("GCS creation time must be timezone aware")
    return OwnTracksSource().parse_raw_key(
        data["name"], storage_created_at=created.astimezone(UTC), storage_generation=generation
    )


def create_worker_app(
    *, processor: NotificationProcessor, subscription: str, bucket: str
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def health() -> Response:
        return Response(status_code=200)

    @app.post("/notifications/owntracks")
    async def ingest(request: Request) -> Response:
        try:
            raw = _notification(await _body(request), subscription=subscription, bucket=bucket)
        except (ValueError, KeyError, TypeError, binascii.Error, RecursionError):
            LOGGER.error("OwnTracks invalid storage notification")
            return Response(status_code=400)
        try:
            ok = await run_in_threadpool(processor.process, raw)
            return Response(status_code=204 if ok else 503)
        except JobAlreadyRunning:
            LOGGER.info("OwnTracks ingestion deferred: shared writer busy")
            return Response(status_code=503)
        except Exception as error:
            LOGGER.error("OwnTracks ingestion failed (%s)", type(error).__name__)
            return Response(status_code=503)

    return app

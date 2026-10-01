"""Google webhook key rotation and exact Cloud Tasks caller verification."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol, cast

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from tink import json_proto_keyset_format, signature

from .models import object_dict
from .webhook import AuthenticationError

KEYSET_URL = "https://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json"


class KeyPrimitive(Protocol):
    def verify(self, signature: bytes, data: bytes) -> None: ...


class TokenVerifier(Protocol):
    def __call__(self, token: str, *, audience: str) -> dict[str, object]: ...


def _keyset() -> str:
    response = httpx.get(KEYSET_URL, timeout=20.0, follow_redirects=False)
    response.raise_for_status()
    return response.text


class TinkSignatures:
    def __init__(
        self,
        *,
        fetch_keyset: Callable[[], str] = _keyset,
        monotonic: Callable[[], float] = time.monotonic,
        refresh_seconds: float = 21600,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("keyset refresh interval must be positive")
        signature.register()
        self._fetch = fetch_keyset
        self._clock = monotonic
        self._ttl = refresh_seconds
        self._expires = 0.0
        self._primitive: KeyPrimitive | None = None
        self._lock = threading.Lock()

    def _get(self, *, force: bool) -> KeyPrimitive:
        with self._lock:
            if force or self._primitive is None or self._clock() >= self._expires:
                try:
                    handle = json_proto_keyset_format.parse_without_secret(self._fetch())
                    self._primitive = cast(
                        KeyPrimitive, handle.primitive(signature.PublicKeyVerify)
                    )
                except Exception:
                    raise RuntimeError("Google Health verification keys are unavailable") from None
                self._expires = self._clock() + self._ttl
            return self._primitive

    def verify(self, signature: bytes, payload: bytes) -> bool:
        for force in (False, True):
            primitive = self._get(force=force)
            try:
                primitive.verify(signature, payload)
                return True
            except Exception:
                pass
        return False


def _verify_token(token: str, *, audience: str) -> dict[str, object]:
    claims = id_token.verify_oauth2_token(token, Request(), audience=audience)  # type: ignore[no-untyped-call]
    return object_dict(claims)


class GoogleTaskIdentity:
    def __init__(
        self, *, audience: str, service_account: str, verify_token: TokenVerifier = _verify_token
    ) -> None:
        if not audience or not service_account:
            raise ValueError("task audience and service account are required")
        self._audience = audience
        self._account = service_account
        self._verify = verify_token

    def authenticate(self, authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("task identity is required")
        try:
            claims = self._verify(authorization[7:], audience=self._audience)
        except Exception:
            raise AuthenticationError("task identity verification failed") from None
        if (
            claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
            or claims.get("aud") != self._audience
            or claims.get("email") != self._account
            or claims.get("email_verified") is not True
        ):
            raise AuthenticationError("unexpected task identity")

"""Google webhook key rotation and exact Cloud Tasks caller verification."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol, cast

import httpx
from tink import json_proto_keyset_format, signature

KEYSET_URL = "https://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json"


class KeyPrimitive(Protocol):
    def verify(self, signature: bytes, data: bytes) -> None: ...


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

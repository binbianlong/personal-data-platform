"""Refresh credentials supplied by the runtime without persisting token values."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlencode

from personal_data_platform.config import secret_config

from .api import HttpTransport, InvalidResponseError, UrllibTransport, request_json


class GoogleOAuth:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        transport: HttpTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not all((client_id, client_secret, refresh_token)):
            raise ValueError("OAuth credentials must not be empty")
        self._body = urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode()
        self._transport = transport or UrllibTransport()
        self._clock = clock
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, transport: HttpTransport | None = None
    ) -> GoogleOAuth:
        values = os.environ if environ is None else environ
        if values.get("PDP_FITBIT_DELIVERY_MODE", "legacy") == "pubsub":
            if any(
                values.get("PDP_FITBIT_OAUTH_" + key)
                for key in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")
            ):
                raise ValueError("legacy OAuth settings conflict with pubsub configuration")
            bundled = secret_config(
                "PDP_FITBIT_OAUTH_CONFIG",
                ("client_id", "client_secret", "refresh_token", "health_user_id"),
                values,
            )
            return cls(
                client_id=bundled["client_id"],
                client_secret=bundled["client_secret"],
                refresh_token=bundled["refresh_token"],
                transport=transport,
            )
        credentials = {}
        for name in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
            key = "PDP_FITBIT_OAUTH_" + name
            value = values.get(key, "")
            if not value.strip():
                raise ValueError(f"{key} is required")
            credentials[name.lower()] = value
        return cls(
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"],
            refresh_token=credentials["refresh_token"],
            transport=transport,
        )

    def __call__(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._expires:
                return self._token
            payload = request_json(
                self._transport,
                "POST",
                "https://oauth2.googleapis.com/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=self._body,
                timeout=30.0,
                oauth=True,
            )
            token, expires = payload.get("access_token"), payload.get("expires_in")
            if (
                not isinstance(token, str)
                or not token.strip()
                or type(expires) is not int
                or expires <= 0
                or payload.get("token_type") != "Bearer"
            ):
                raise InvalidResponseError("OAuth returned an invalid access token response")
            self._token = token
            self._expires = now + max(0, expires - 60)
            return token

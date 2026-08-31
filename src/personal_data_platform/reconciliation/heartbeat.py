"""Publish a dead-man's-switch heartbeat only after successful reconciliation."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.request import Request, urlopen


def publish_http_heartbeat(url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> None:
    body = json.dumps(payload, sort_keys=True).encode()
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "personal-data-platform/1"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 -- configured HTTPS endpoint
        if not 200 <= response.status < 300:
            raise RuntimeError(f"heartbeat endpoint returned HTTP {response.status}")


HeartbeatPublisher = Callable[[dict[str, Any]], None]

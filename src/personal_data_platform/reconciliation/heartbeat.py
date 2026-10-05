"""Publish a dead-man's-switch heartbeat only after successful reconciliation."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.request import Request, urlopen

from personal_data_platform.config import secret_config


def daily_heartbeat_urls() -> dict[str, str]:
    urls = secret_config("PDP_HEARTBEAT_CONFIG", ("daily", "app-in-focus", "app-usage"))
    if any(not url.startswith("https://") for url in urls.values()):
        raise ValueError("PDP_HEARTBEAT_CONFIG URLs must use HTTPS")
    return urls


def publish_http_heartbeat(url: str, payload: dict[str, object], *, timeout: float = 10.0) -> None:
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


HeartbeatPublisher = Callable[[dict[str, object]], None]

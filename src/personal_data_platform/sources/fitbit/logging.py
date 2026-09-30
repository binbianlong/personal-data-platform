"""Fitbit application logs carry severity independently of their output stream."""

from __future__ import annotations

import json
import logging
import os
import sys


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        for key in ("event", "status", "summary"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Configure only the Fitbit namespace, once, without changing other jobs."""
    name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = logging.getLevelNamesMapping().get(name)
    if level is None:
        raise ValueError("LOG_LEVEL must be a standard logging level")
    logger = logging.getLogger("personal_data_platform.sources.fitbit")
    handler = next(
        (
            item for item in logger.handlers
            if isinstance(item, logging.StreamHandler) and isinstance(item.formatter, _JSONFormatter)
        ), None
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JSONFormatter())
        logger.addHandler(handler)
    else:
        handler.setStream(sys.stderr)
    logger.setLevel(level)
    logger.propagate = False

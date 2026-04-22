"""Structured JSON logging configuration for the API service."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any, Dict

LOG_PATH: str = os.environ.get("LOG_PATH", "/app/logs/app.log")
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()
SERVICE_NAME: str = os.environ.get("SERVICE_NAME", "api")

MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT: int = 5


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": SERVICE_NAME,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for attr in ("request_id",):
            val = getattr(record, attr, None)
            if val is not None:
                payload[attr] = val
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> logging.Logger:
    """Configure rotating file + stream handlers; return the service logger."""
    path = Path(LOG_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # unwritable path — PermissionError surfaces at handler creation below

    formatter = _JsonFormatter()

    file_handler = logging.handlers.RotatingFileHandler(
        str(path), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(stream_handler)

    return logging.getLogger(SERVICE_NAME)

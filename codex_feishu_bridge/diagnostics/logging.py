"""Structured, rotating, body-free service logging."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from ..runtime_storage import utc_now


_FORBIDDEN = {"token", "secret", "body", "content", "message", "payload", "prompt", "text"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        if not isinstance(fields, dict):
            fields = {}
        safe = {
            key: value
            for key, value in fields.items()
            if str(key).lower() not in _FORBIDDEN
        }
        return json.dumps(
            {
                "timestamp": utc_now(),
                "level": record.levelname,
                "event": record.getMessage(),
                "fields": safe,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )


def configure_logging(path: Path, *, max_bytes: int = 10_000_000, backups: int = 10) -> logging.Logger:
    path.resolve().parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("codex_feishu_bridge")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger

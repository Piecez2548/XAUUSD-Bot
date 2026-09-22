"""Structured application logging with conservative secret redaction."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        output = json.dumps(payload, ensure_ascii=False, default=str)
        for secret in self._secrets:
            output = output.replace(secret, "[REDACTED]")
        return output


class RedactingTextFormatter(logging.Formatter):
    """Redact secrets from the complete formatted record, including tracebacks."""

    def __init__(self, fmt: str, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        for secret in self._secrets:
            output = output.replace(secret, "[REDACTED]")
        return output


def configure_logging(
    log_directory: Path,
    level: str = "INFO",
    *,
    secrets: tuple[str, ...] = (),
) -> logging.Logger:
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("xauusd_ai_trader")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    file_handler = logging.FileHandler(log_directory / "phase1.jsonl", encoding="utf-8")
    file_handler.setFormatter(JsonFormatter(secrets))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(RedactingTextFormatter("%(levelname)s %(message)s", secrets))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

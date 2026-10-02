"""Structured JSON logging with secret redaction and request/execution correlation."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.masking import mask_data, mask_inline

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
execution_id_var: ContextVar[str | None] = ContextVar("execution_id", default=None)
org_id_var: ContextVar[str | None] = ContextVar("org_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": mask_inline(record.getMessage()),
        }
        for var, key in ((request_id_var, "request_id"), (execution_id_var, "execution_id"), (org_id_var, "org_id")):
            value = var.get()
            if value:
                payload[key] = value
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        if extras:
            payload.update(mask_data(extras))
        if record.exc_info:
            payload["exc"] = mask_inline(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class RedactingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return mask_inline(super().format(record))


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(RedactingJsonFormatter())
    else:
        handler.setFormatter(RedactingTextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "asyncio", "aiosmtplib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

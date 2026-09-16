"""Structured (JSON) logging with request correlation.

In a distributed system one user action touches Nginx, an API instance, RabbitMQ and a
worker. A shared request_id in every log line is what lets you follow that action across
machines with a simple `grep`.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, instance_id: str) -> None:
        super().__init__()
        self.service = service
        self.instance_id = instance_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service,
            "instance": self.instance_id,
        }
        if request_id := request_id_ctx.get():
            payload["request_id"] = request_id
        # Anything passed via `extra={...}` becomes a top-level field.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, *, json_logs: bool, service: str, instance_id: str) -> None:
    handler = logging.StreamHandler(sys.stdout)  # containers log to stdout; Docker collects it
    if json_logs:
        handler.setFormatter(JsonFormatter(service, instance_id))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Uvicorn's own access log duplicates our middleware access log.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("aio_pika", "aiormq", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

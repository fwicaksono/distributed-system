"""Producer side of the task queue."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection

from app.core import rabbitmq
from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.core.metrics import TASKS_PUBLISHED
from app.schemas.task import TaskMessage

logger = logging.getLogger(__name__)


class TaskPublisher(Protocol):
    async def publish(self, message: TaskMessage, *, priority: int = 5) -> None: ...


class RabbitTaskPublisher:
    """Publishes persistent messages with publisher confirms.

    delivery_mode=PERSISTENT + durable queue => the message is written to disk and survives
    a broker restart. Publisher confirms => `publish()` only returns after RabbitMQ has
    taken responsibility for the message. Without confirms, a message can vanish in a
    TCP buffer while the API already answered "202 Accepted".
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._exchange is not None:
                return
            self._connection = await rabbitmq.connect(self.settings)
            self._channel = await self._connection.channel(publisher_confirms=True)
            self._exchange = await rabbitmq.declare_topology(self._channel)
            logger.info("rabbitmq publisher connected")

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self._connection = self._channel = self._exchange = None

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed

    async def publish(self, message: TaskMessage, *, priority: int = 5) -> None:
        try:
            if self._exchange is None:
                # Lazy (re)connect: the API can start even if RabbitMQ is down.
                await self.connect()
            assert self._exchange is not None
            await asyncio.wait_for(
                self._exchange.publish(
                    aio_pika.Message(
                        body=message.model_dump_json().encode(),
                        content_type="application/json",
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        message_id=f"{message.task_id}",
                        correlation_id=message.request_id,
                        priority=priority,
                        headers={rabbitmq.RETRY_COUNT_HEADER: 0},
                    ),
                    routing_key=rabbitmq.PROCESS_ROUTING_KEY,
                    mandatory=True,  # fail if no queue is bound (misconfiguration)
                ),
                timeout=self.settings.rabbitmq_publish_timeout_seconds,
            )
            TASKS_PUBLISHED.labels("ok").inc()
        except Exception as exc:
            TASKS_PUBLISHED.labels("error").inc()
            logger.error("failed to publish task", extra={"task_id": str(message.task_id),
                                                          "error": repr(exc)})
            raise DependencyUnavailableError("Task queue is unavailable, try again later") from exc

"""RabbitMQ consumer: at-least-once delivery + idempotent processing.

Delivery guarantee
------------------
RabbitMQ gives *at-least-once* delivery: a message is redelivered if the worker dies
before acking. So the same task CAN arrive twice. We make processing idempotent with a
database claim (see TaskRepository.claim) instead of pretending duplicates can't happen.

Message lifecycle
-----------------
    receive ──> claim in PostgreSQL ──┬─ claimed ──> run ──┬─ ok ─────────> COMPLETED, ACK
                                      │                    ├─ transient ──> retry queue N, ACK
                                      │                    └─ permanent ──> FAILED, NACK -> DLQ
                                      ├─ already completed/failed ──────────> ACK (duplicate)
                                      └─ leased by another worker ──────────> retry queue, ACK

Ack only AFTER the database commit. Acking first and crashing before commit loses the task.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from concurrent.futures import ProcessPoolExecutor

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import rabbitmq
from app.core.config import Settings
from app.core.logging import request_id_ctx
from app.core.metrics import WORKER_IN_FLIGHT, WORKER_MESSAGES, WORKER_TASK_DURATION
from app.models.task import TaskStatus
from app.repositories.task_repository import TaskRepository
from app.schemas.task import TaskMessage
from app.services.cache import TaskCache
from app.workers.handlers import PermanentTaskError, TransientTaskError, run_task

logger = logging.getLogger(__name__)


class TaskConsumer:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        retry_exchange: AbstractExchange,
        cpu_pool: ProcessPoolExecutor,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.cache = TaskCache(
            redis,
            ttl_seconds=settings.cache_task_ttl_seconds,
            jitter_seconds=settings.cache_ttl_jitter_seconds,
        )
        self.retry_exchange = retry_exchange
        self.cpu_pool = cpu_pool
        self.worker_id = settings.instance_id

    async def handle(self, message: AbstractIncomingMessage) -> None:
        WORKER_IN_FLIGHT.inc()
        try:
            await self._handle(message)
        finally:
            WORKER_IN_FLIGHT.dec()

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        try:
            task_message = TaskMessage.model_validate_json(message.body)
        except ValidationError:
            logger.error("malformed message, dead-lettering", extra={"body": message.body[:200]})
            WORKER_MESSAGES.labels("dead_lettered").inc()
            await message.reject(requeue=False)  # NACK without requeue -> DLX -> DLQ
            return

        token = request_id_ctx.set(task_message.request_id)
        task_id = task_message.task_id
        retry_count = int(str((message.headers or {}).get(rabbitmq.RETRY_COUNT_HEADER, 0) or 0))
        log_extra = {"task_id": str(task_id), "retry": retry_count,
                     "redelivered": message.redelivered}
        try:
            async with self.session_factory() as session:
                repo = TaskRepository(session)
                task = await repo.claim(task_id, self.settings.worker_processing_lease_seconds)
                if task is None:
                    await session.rollback()
                    await self._handle_unclaimable(message, repo, task_id, retry_count)
                    return
                await repo.add_log(task_id, "INFO", f"processing attempt {task.attempts}",
                                   self.worker_id)
                payload = dict(task.payload)
                await session.commit()  # the claim must be visible to other workers NOW
            await self.cache.invalidate(task_id)

            logger.info("task claimed", extra=log_extra)
            kind = str(payload.get("kind", "unknown"))
            started = time.perf_counter()
            try:
                output = await run_task(str(task_id), payload, self.cpu_pool)
            except PermanentTaskError as exc:
                await self._fail(task_id, str(exc))
                WORKER_MESSAGES.labels("dead_lettered").inc()
                await message.reject(requeue=False)
                return
            except (TransientTaskError, TimeoutError, OSError) as exc:
                await self._retry_or_fail(message, task_id, retry_count, str(exc))
                return

            duration = time.perf_counter() - started
            WORKER_TASK_DURATION.labels(kind).observe(duration)
            async with self.session_factory() as session:
                repo = TaskRepository(session)
                await repo.complete(task_id, output=output, duration_ms=int(duration * 1000),
                                    worker_id=self.worker_id)
                await repo.add_log(task_id, "INFO", "completed", self.worker_id)
                await session.commit()
            await self.cache.invalidate(task_id)
            await message.ack()  # only now is the message removed from RabbitMQ
            WORKER_MESSAGES.labels("completed").inc()
            logger.info("task completed", extra={**log_extra, "duration_ms": int(duration * 1000)})
        except Exception:
            # Unexpected error (e.g. PostgreSQL down). Don't ack: requeue so another worker
            # (or this one, later) retries. Sleep first to avoid a hot redelivery loop.
            logger.exception("unexpected error handling message", extra=log_extra)
            await asyncio.sleep(1)
            if not message.processed:
                await message.nack(requeue=True)
        finally:
            request_id_ctx.reset(token)

    async def _handle_unclaimable(
        self,
        message: AbstractIncomingMessage,
        repo: TaskRepository,
        task_id: uuid.UUID,
        retry_count: int,
    ) -> None:
        current = await repo.get(task_id)
        if current is None or current.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            # Task deleted, or duplicate delivery of already-finished work: drop it.
            WORKER_MESSAGES.labels("skipped").inc()
            await message.ack()
            return
        # Another worker holds a live lease (duplicate message), or the previous worker
        # crashed and its lease hasn't expired yet. Check again later.
        logger.info("task leased elsewhere, deferring", extra={"task_id": str(task_id)})
        await self._retry_or_fail(message, task_id, retry_count, "lease held by another worker",
                                  mark_state=False)

    async def _retry_or_fail(
        self,
        message: AbstractIncomingMessage,
        task_id: uuid.UUID,
        retry_count: int,
        error: str,
        *,
        mark_state: bool = True,
    ) -> None:
        next_attempt = retry_count + 1
        if next_attempt > len(rabbitmq.RETRY_DELAYS_MS) and not mark_state:
            # A lease is still held after all retry delays: the holder is alive and the
            # message is a duplicate. Never fail a task another worker is running.
            WORKER_MESSAGES.labels("skipped").inc()
            await message.ack()
            return
        if next_attempt > len(rabbitmq.RETRY_DELAYS_MS):
            await self._fail(task_id, f"retries exhausted: {error}")
            WORKER_MESSAGES.labels("dead_lettered").inc()
            await message.reject(requeue=False)
            return

        if mark_state:
            async with self.session_factory() as session:
                repo = TaskRepository(session)
                await repo.release_for_retry(task_id, error)
                await repo.add_log(task_id, "WARNING", f"retry {next_attempt} scheduled: {error}",
                                   self.worker_id)
                await session.commit()
            await self.cache.invalidate(task_id)

        # Publish the retry copy (with confirm) BEFORE acking the original. If we crash in
        # between, the original is redelivered: a duplicate, which claim() handles.
        await self.retry_exchange.publish(
            aio_pika.Message(
                body=message.body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                headers={rabbitmq.RETRY_COUNT_HEADER: next_attempt},
            ),
            routing_key=f"retry.{next_attempt}",
        )
        await message.ack()
        WORKER_MESSAGES.labels("retried").inc()
        logger.warning(
            "task scheduled for retry",
            extra={"task_id": str(task_id), "retry": next_attempt,
                   "delay_ms": rabbitmq.RETRY_DELAYS_MS[next_attempt - 1], "error": error},
        )

    async def _fail(self, task_id: uuid.UUID, error: str) -> None:
        async with self.session_factory() as session:
            repo = TaskRepository(session)
            await repo.fail(task_id, error)
            await repo.add_log(task_id, "ERROR", error, self.worker_id)
            await session.commit()
        await self.cache.invalidate(task_id)
        logger.error("task failed", extra={"task_id": str(task_id), "error": error})

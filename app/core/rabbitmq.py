"""RabbitMQ topology and connection helpers.

Topology (declared idempotently by both API and workers on startup):

    [tasks] (direct exchange)
        └─ routing key "task.process" ──> queue "tasks.process" ──> workers (competing consumers)
                                               │ nack(requeue=False)
                                               v
    [tasks.dlx] ── "task.dead" ──────────> queue "tasks.dead-letter"  (inspect manually)

    [tasks.retry] (direct exchange)
        ├─ "retry.1" ──> queue "tasks.retry.1" (TTL  5s) ─┐
        ├─ "retry.2" ──> queue "tasks.retry.2" (TTL 30s) ─┼─ on expiry, dead-letter back to
        └─ "retry.3" ──> queue "tasks.retry.3" (TTL 120s)─┘  [tasks] with "task.process"

Retry queues give *delayed* retries with exponential backoff without any plugin: a
message sits in a queue with no consumers until its TTL expires, then RabbitMQ
dead-letters it back to the main exchange.

NOTE: queue arguments are immutable. Changing e.g. a TTL requires deleting the queue
first, otherwise declare fails with PRECONDITION_FAILED.
"""

from __future__ import annotations

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection

from app.core.config import Settings

TASKS_EXCHANGE = "tasks"
RETRY_EXCHANGE = "tasks.retry"
DEAD_LETTER_EXCHANGE = "tasks.dlx"

PROCESS_QUEUE = "tasks.process"
DEAD_LETTER_QUEUE = "tasks.dead-letter"
PROCESS_ROUTING_KEY = "task.process"
DEAD_LETTER_ROUTING_KEY = "task.dead"

# Delay before retry N (1-based). len() == maximum number of retries.
RETRY_DELAYS_MS: tuple[int, ...] = (5_000, 30_000, 120_000)

RETRY_COUNT_HEADER = "x-retry-count"


def retry_queue_name(attempt: int) -> str:
    return f"{PROCESS_QUEUE.split('.')[0]}.retry.{attempt}"


async def connect(settings: Settings) -> AbstractRobustConnection:
    # connect_robust re-establishes the connection and re-declares topology/consumers
    # automatically after a broker restart or network blip.
    return await aio_pika.connect_robust(
        settings.rabbitmq_url,
        client_properties={"connection_name": f"{settings.app_name}:{settings.instance_id}"},
        timeout=10,
        heartbeat=30,
    )


async def declare_topology(channel: AbstractChannel) -> AbstractExchange:
    """Declare exchanges/queues/bindings. Safe to call repeatedly. Returns the main exchange."""
    tasks = await channel.declare_exchange(
        TASKS_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
    )
    retry = await channel.declare_exchange(
        RETRY_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
    )
    dlx = await channel.declare_exchange(
        DEAD_LETTER_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
    )

    process_queue = await channel.declare_queue(
        PROCESS_QUEUE,
        durable=True,  # queue definition survives broker restart
        arguments={
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
            "x-dead-letter-routing-key": DEAD_LETTER_ROUTING_KEY,
        },
    )
    await process_queue.bind(tasks, routing_key=PROCESS_ROUTING_KEY)

    dead_letter_queue = await channel.declare_queue(DEAD_LETTER_QUEUE, durable=True)
    await dead_letter_queue.bind(dlx, routing_key=DEAD_LETTER_ROUTING_KEY)

    for attempt, delay_ms in enumerate(RETRY_DELAYS_MS, start=1):
        queue = await channel.declare_queue(
            retry_queue_name(attempt),
            durable=True,
            arguments={
                "x-message-ttl": delay_ms,
                "x-dead-letter-exchange": TASKS_EXCHANGE,
                "x-dead-letter-routing-key": PROCESS_ROUTING_KEY,
            },
        )
        await queue.bind(retry, routing_key=f"retry.{attempt}")

    return tasks

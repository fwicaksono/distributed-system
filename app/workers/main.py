"""Worker process entrypoint:  python -m app.workers.main

Concurrency model
-----------------
* One OS process per worker container (scale horizontally with more containers/EC2s).
* Inside it, one asyncio event loop handles up to WORKER_CONCURRENCY messages at once.
  RabbitMQ enforces this with prefetch (basic.qos): it never pushes more unacked
  messages than that to this consumer. That is back-pressure.
* CPU-bound work goes to a small process pool (true parallelism across cores).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from concurrent.futures import ProcessPoolExecutor

from aio_pika.abc import AbstractIncomingMessage
from prometheus_client import start_http_server

from app import __version__
from app.core import rabbitmq
from app.core.config import get_settings
from app.core.database import create_engine, create_session_factory, ping
from app.core.logging import configure_logging
from app.core.redis import create_redis
from app.utils.retry import retry_async
from app.workers.consumer import TaskConsumer

logger = logging.getLogger("app.worker")


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json, service="worker",
                      instance_id=settings.instance_id)
    start_http_server(settings.worker_metrics_port)  # Prometheus scrapes :9101/metrics

    total_retry_window = sum(rabbitmq.RETRY_DELAYS_MS) / 1000
    if settings.worker_processing_lease_seconds >= total_retry_window:
        # A crashed worker's task is only re-claimable after its lease expires; the retry
        # queues must keep the redelivered message alive longer than that.
        raise SystemExit(
            f"WORKER_PROCESSING_LEASE_SECONDS must be < {total_retry_window:.0f}s "
            "(sum of RETRY_DELAYS_MS)"
        )

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    redis = create_redis(settings)
    cpu_pool = ProcessPoolExecutor(max_workers=settings.worker_cpu_pool_size)

    # Dependencies may start after us (compose, reboots): wait with backoff instead of crashing.
    await retry_async(lambda: ping(engine), name="postgres")
    connection = await retry_async(lambda: rabbitmq.connect(settings), name="rabbitmq")

    channel = await connection.channel(publisher_confirms=True)
    await channel.set_qos(prefetch_count=settings.worker_concurrency)
    await rabbitmq.declare_topology(channel)
    retry_exchange = await channel.get_exchange(rabbitmq.RETRY_EXCHANGE)
    queue = await channel.get_queue(rabbitmq.PROCESS_QUEUE)

    consumer = TaskConsumer(settings, session_factory, redis, retry_exchange, cpu_pool)
    in_flight: set[asyncio.Task[None]] = set()

    async def on_message(message: AbstractIncomingMessage) -> None:
        # aio-pika awaits this callback, but prefetch already bounds concurrency; tracking
        # tasks lets shutdown wait for them.
        task = asyncio.current_task()
        if task is not None:
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
        await consumer.handle(message)

    consumer_tag = await queue.consume(on_message, no_ack=False)
    logger.info("worker started", extra={"version": __version__,
                                         "concurrency": settings.worker_concurrency})

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    # Graceful shutdown: stop receiving new messages, finish in-flight ones, then close.
    # Anything still unacked when the connection closes is requeued by RabbitMQ — no loss.
    logger.info("worker stopping", extra={"in_flight": len(in_flight)})
    await queue.cancel(consumer_tag)
    if in_flight:
        await asyncio.wait(in_flight, timeout=25)  # < docker stop_grace_period (30s)
    await connection.close()
    await redis.aclose()
    await engine.dispose()
    cpu_pool.shutdown(wait=False, cancel_futures=True)
    logger.info("worker stopped")



def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

"""The actual "business logic" executed by workers. Intentionally trivial.

This module is pure: no RabbitMQ, no database. That makes it trivially testable and
shows the separation between *transport* (consumer.py) and *work* (handlers.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from app.schemas.task import TaskPayload


class TransientTaskError(Exception):
    """Temporary failure (timeout, dependency hiccup) — worth retrying."""


class PermanentTaskError(Exception):
    """Retrying cannot help (invalid payload, business rule) — dead-letter immediately."""


def cpu_bound_hash(iterations: int, seed: str) -> str:
    """Runs in a separate *process*. The GIL means threads cannot run Python bytecode in
    parallel; processes each have their own interpreter and GIL, so they use all cores."""
    digest = seed.encode()
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()
    return digest.hex()


async def run_task(
    task_id: str, raw_payload: dict[str, Any], cpu_pool: ProcessPoolExecutor
) -> dict[str, Any]:
    try:
        payload = TaskPayload.model_validate(raw_payload)
    except ValueError as exc:
        raise PermanentTaskError(f"invalid payload: {exc}") from exc

    started = time.perf_counter()

    if payload.failure_rate and random.random() < payload.failure_rate:
        raise TransientTaskError("simulated transient failure")

    if payload.kind == "sleep":
        # I/O-bound: awaiting releases the event loop, so one worker process can run
        # `worker_concurrency` of these at the same time on a single core (concurrency).
        await asyncio.sleep(payload.duration_seconds)
        output: dict[str, Any] = {"slept_seconds": payload.duration_seconds}
    else:
        # CPU-bound: running this on the event loop would freeze heartbeats and every other
        # in-flight task. Offload to a process pool (parallelism).
        loop = asyncio.get_running_loop()
        digest = await loop.run_in_executor(
            cpu_pool, cpu_bound_hash, payload.iterations, task_id
        )
        output = {"iterations": payload.iterations, "digest": digest}

    output["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return output

"""Exponential backoff with full jitter.

Without jitter, 3 API servers that lose RabbitMQ at the same moment retry at the same
moments too (1s, 2s, 4s...), hammering the recovering service in synchronized waves
("thundering herd"). Random jitter spreads the retries out.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


def backoff_delay(attempt: int, *, base: float = 0.5, cap: float = 30.0) -> float:
    """Full-jitter delay for a 0-based attempt number."""
    return random.uniform(0, min(cap, base * (2**attempt)))


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    name: str,
    attempts: int = 10,
    base: float = 0.5,
    cap: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    for attempt in range(attempts):
        try:
            return await operation()
        except retry_on as exc:
            if attempt == attempts - 1:
                raise
            delay = backoff_delay(attempt, base=base, cap=cap)
            logger.warning(
                "retrying after failure",
                extra={"operation": name, "attempt": attempt + 1, "delay_s": round(delay, 2),
                       "error": repr(exc)},
            )
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")

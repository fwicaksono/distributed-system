"""Cache-aside for task reads, with TTL jitter, stampede protection and fail-open behaviour.

    read:   Redis GET ──hit──> return
                 └──miss──> acquire short lock ──> PostgreSQL ──> Redis SET (TTL) ──> return
    write:  PostgreSQL UPDATE/DELETE ──> Redis DEL  (invalidate, never "update" the cache)

Redis is never the source of truth. Every Redis error is logged, counted and swallowed so
the request falls through to PostgreSQL: a Redis outage makes us slower, not broken.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.metrics import CACHE_OPERATIONS
from app.schemas.task import TaskRead

logger = logging.getLogger(__name__)

KEY_VERSION = "v1"  # bump when TaskRead's shape changes: old entries are simply ignored


def task_key(task_id: uuid.UUID) -> str:
    return f"task:{KEY_VERSION}:{task_id}"


class TaskCache:
    def __init__(self, redis: Redis, *, ttl_seconds: int, jitter_seconds: int) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.jitter_seconds = jitter_seconds

    async def get(self, task_id: uuid.UUID) -> TaskRead | None:
        try:
            raw = await self.redis.get(task_key(task_id))
        except (RedisError, OSError) as exc:
            CACHE_OPERATIONS.labels("task", "error").inc()
            logger.warning("cache get failed", extra={"error": repr(exc)})
            return None
        if raw is None:
            CACHE_OPERATIONS.labels("task", "miss").inc()
            return None
        CACHE_OPERATIONS.labels("task", "hit").inc()
        return TaskRead.model_validate_json(raw)

    async def set(self, task: TaskRead) -> None:
        # Jitter: keys written together don't all expire in the same second.
        ttl = self.ttl_seconds + random.randint(0, self.jitter_seconds)
        try:
            await self.redis.set(task_key(task.id), task.model_dump_json(), ex=ttl)
        except (RedisError, OSError) as exc:
            logger.warning("cache set failed", extra={"error": repr(exc)})

    async def invalidate(self, task_id: uuid.UUID) -> None:
        try:
            await self.redis.delete(task_key(task_id))
        except (RedisError, OSError) as exc:
            # Stale entry survives at most TTL seconds. Acceptable for this data.
            logger.warning("cache invalidate failed", extra={"error": repr(exc)})

    async def get_or_load(
        self, task_id: uuid.UUID, loader: Callable[[], Awaitable[TaskRead | None]]
    ) -> TaskRead | None:
        cached = await self.get(task_id)
        if cached is not None:
            return cached

        # Stampede protection: when a hot key expires, only the lock holder queries the
        # database; others wait briefly and re-read the cache.
        lock_key = f"lock:{task_key(task_id)}"
        token = uuid.uuid4().hex
        acquired = await self._try_lock(lock_key, token)
        if not acquired:
            await asyncio.sleep(0.05)
            cached = await self.get(task_id)
            if cached is not None:
                return cached

        try:
            value = await loader()
            if value is not None:
                await self.set(value)
            return value
        finally:
            if acquired:
                await self._release_lock(lock_key, token)

    async def _try_lock(self, key: str, token: str) -> bool:
        try:
            return bool(await self.redis.set(key, token, nx=True, px=2000))
        except (RedisError, OSError):
            return False

    _RELEASE_SCRIPT = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end"
        " return 0"
    )

    async def _release_lock(self, key: str, token: str) -> None:
        # Compare-and-delete atomically: never delete a lock someone else now owns.
        with contextlib.suppress(RedisError, OSError):
            await self.redis.eval(self._RELEASE_SCRIPT, 1, key, token)  # type: ignore[misc]

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from redis.exceptions import ConnectionError as RedisConnectionError

from app.models.task import TaskStatus
from app.schemas.task import TaskRead
from app.services.cache import TaskCache, task_key


def make_task() -> TaskRead:
    now = datetime.now(UTC)
    return TaskRead(
        id=uuid.uuid4(), owner_id=uuid.uuid4(), title="t", description=None, priority=5,
        payload={}, status=TaskStatus.PENDING, attempts=0, last_error=None,
        created_at=now, updated_at=now,
    )


async def test_cache_aside_miss_then_hit(redis) -> None:
    cache = TaskCache(redis, ttl_seconds=60, jitter_seconds=5)
    task = make_task()
    calls = 0

    async def loader() -> TaskRead:
        nonlocal calls
        calls += 1
        return task

    assert await cache.get_or_load(task.id, loader) == task  # miss -> DB
    assert await cache.get_or_load(task.id, loader) == task  # hit  -> no DB
    assert calls == 1

    ttl = await redis.ttl(task_key(task.id))
    assert 60 <= ttl <= 65  # TTL + jitter


async def test_invalidate_forces_reload(redis) -> None:
    cache = TaskCache(redis, ttl_seconds=60, jitter_seconds=0)
    task = make_task()
    await cache.set(task)
    await cache.invalidate(task.id)
    assert await cache.get(task.id) is None


async def test_missing_rows_are_not_cached(redis) -> None:
    cache = TaskCache(redis, ttl_seconds=60, jitter_seconds=0)
    task_id = uuid.uuid4()

    async def loader() -> None:
        return None

    assert await cache.get_or_load(task_id, loader) is None
    assert await redis.exists(task_key(task_id)) == 0


class BrokenRedis:
    """Simulates Redis being down: every command raises."""

    def __getattr__(self, _name: str):
        async def fail(*_args, **_kwargs):
            raise RedisConnectionError("connection refused")

        return fail


async def test_redis_outage_falls_through_to_database() -> None:
    cache = TaskCache(BrokenRedis(), ttl_seconds=60, jitter_seconds=0)  # type: ignore[arg-type]
    task = make_task()

    async def loader() -> TaskRead:
        return task

    assert await cache.get_or_load(task.id, loader) == task

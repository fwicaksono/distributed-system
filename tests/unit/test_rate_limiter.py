from __future__ import annotations

from app.services.rate_limiter import RateLimiter
from tests.unit.test_cache import BrokenRedis


class BrokenPipelineRedis(BrokenRedis):
    def pipeline(self, *args, **kwargs):
        return BrokenPipeline()


class BrokenPipeline:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def incr(self, *_args):
        return self

    def expire(self, *_args):
        return self

    async def execute(self):
        from redis.exceptions import ConnectionError as RedisConnectionError

        raise RedisConnectionError("connection refused")


async def test_allows_up_to_limit_then_blocks(redis) -> None:
    limiter = RateLimiter(redis, limit=3, window_seconds=60)
    decisions = [await limiter.hit("ip:1.2.3.4") for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[2].remaining == 0
    assert 0 < decisions[3].reset_after_seconds <= 60


async def test_limits_are_per_identity(redis) -> None:
    limiter = RateLimiter(redis, limit=1, window_seconds=60)
    assert (await limiter.hit("ip:a")).allowed
    assert (await limiter.hit("ip:b")).allowed
    assert not (await limiter.hit("ip:a")).allowed


async def test_fails_open_when_redis_is_down() -> None:
    limiter = RateLimiter(BrokenPipelineRedis(), limit=1, window_seconds=60)  # type: ignore[arg-type]
    assert (await limiter.hit("ip:a")).allowed
    assert (await limiter.hit("ip:a")).allowed

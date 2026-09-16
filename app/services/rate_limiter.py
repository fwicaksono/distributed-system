"""Fixed-window rate limiter backed by Redis.

Why Redis and not an in-memory dict? With 3 API servers behind a round-robin load
balancer, an in-memory counter would allow 3x the limit (each server counts separately).
Shared state for rate limiting must live outside the stateless API processes.

Trade-off: a fixed window allows up to 2x the limit across a window boundary. A sliding
window log or token bucket (Lua script) is more precise but costs more Redis work.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int


class RateLimiter:
    def __init__(self, redis: Redis, *, limit: int, window_seconds: int) -> None:
        self.redis = redis
        self.limit = limit
        self.window_seconds = window_seconds

    async def hit(self, identity: str) -> RateLimitDecision:
        now = int(time.time())
        window_start = now - (now % self.window_seconds)
        reset_after = self.window_seconds - (now - window_start)
        key = f"ratelimit:{identity}:{window_start}"
        try:
            # MULTI/EXEC pipeline: INCR and EXPIRE are applied atomically.
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self.window_seconds + 1)
                count, _ = await pipe.execute()
        except (RedisError, OSError) as exc:
            # Fail OPEN: a Redis outage should not turn into a full API outage.
            # (A payments API might choose fail CLOSED instead — it's a business decision.)
            logger.warning("rate limiter unavailable, allowing request", extra={"error": repr(exc)})
            return RateLimitDecision(True, self.limit, self.limit, reset_after)

        remaining = max(0, self.limit - int(count))
        return RateLimitDecision(int(count) <= self.limit, self.limit, remaining, reset_after)

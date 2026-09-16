"""Redis client factory.

Short socket timeouts are deliberate: Redis is an optimisation (cache, rate limit), not
the source of truth. If Redis is slow or down, we would rather skip it after 500 ms than
make every request hang.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import Settings


def create_redis(settings: Settings) -> Redis:
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password.get_secret_value() or None,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=30,
        decode_responses=True,
        max_connections=50,
    )

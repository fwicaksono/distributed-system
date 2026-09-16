"""Health endpoints.

/health/live  — "is this process alive?"  Used by Docker HEALTHCHECK / restart decisions.
                Checks NOTHING external: if PostgreSQL is down, restarting the API won't fix it.
/health/ready — "should this instance receive traffic?" Checks critical dependencies.

Readiness only fails on PostgreSQL. Redis and RabbitMQ failures are reported as
"degraded": the API can still serve reads without them. If readiness failed on every
dependency, one Redis outage would mark ALL API servers unhealthy at once — the health
check itself would turn a partial failure into a total outage.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request, Response, status

from app import __version__
from app.core.database import ping as db_ping
from app.schemas.common import DependencyHealth, HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


async def _check(fn: Callable[[], Awaitable[object]]) -> DependencyHealth:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(2.0):  # a hung dependency must not hang the health check
            await fn()
        return DependencyHealth(
            status="ok", latency_ms=round((time.perf_counter() - started) * 1000, 2)
        )
    except Exception as exc:
        return DependencyHealth(status="error", error=type(exc).__name__)


@router.get("/live", response_model=HealthResponse)
async def live(request: Request) -> HealthResponse:
    return HealthResponse(
        status="ok", instance=request.app.state.settings.instance_id, version=__version__
    )


@router.get("/ready", response_model=HealthResponse)
async def ready(request: Request, response: Response) -> HealthResponse:
    state = request.app.state

    async def rabbit() -> None:
        if not state.publisher.is_connected:
            await state.publisher.connect()

    db, cache, broker = await asyncio.gather(
        _check(lambda: db_ping(state.engine)),
        _check(state.redis.ping),
        _check(rabbit),
    )
    checks = {"postgres": db, "redis": cache, "rabbitmq": broker}

    if db.status == "error":
        overall = "unavailable"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif any(c.status == "error" for c in checks.values()):
        overall = "degraded"
    else:
        overall = "ok"
    return HealthResponse(
        status=overall, instance=state.settings.instance_id, version=__version__, checks=checks
    )

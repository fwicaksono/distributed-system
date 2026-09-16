"""FastAPI application factory.

Run: uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers

A factory (instead of a module-level `app`) means importing this module never reads
environment variables — tests and tooling can import it freely.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from app import __version__
from app.api.router import api_router
from app.api.routes import health
from app.core.config import Settings, get_settings
from app.core.database import create_engine, create_session_factory
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.metrics import DB_POOL_CHECKED_OUT, DB_POOL_SIZE
from app.core.redis import create_redis
from app.middleware.request_context import RequestContextMiddleware
from app.services.task_publisher import RabbitTaskPublisher

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create shared, long-lived resources ONCE per process and close them on shutdown.

    Per-request connections would add a TCP + auth handshake to every request and exhaust
    PostgreSQL's max_connections under load.
    """
    settings: Settings = app.state.settings
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.redis = create_redis(settings)
    app.state.publisher = RabbitTaskPublisher(settings)
    try:
        await app.state.publisher.connect()
    except Exception as exc:
        # Start anyway: reads keep working; publishing reconnects lazily (see publisher).
        logger.warning("rabbitmq unavailable at startup", extra={"error": repr(exc)})

    logger.info("api started", extra={"version": __version__, "environment": settings.environment})
    yield

    # Graceful shutdown: uvicorn stops accepting connections, drains in-flight requests,
    # then we close pools. Docker sends SIGTERM and waits `stop_grace_period` before SIGKILL.
    logger.info("api shutting down")
    await app.state.publisher.close()
    await app.state.redis.aclose()
    await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        settings.log_level,
        json_logs=settings.log_json,
        service="api",
        instance_id=settings.instance_id,
    )

    app = FastAPI(
        title="Distributed Task Platform",
        version=__version__,
        lifespan=lifespan,
        # Hide interactive docs in production; the schema is still generated for clients.
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
    )
    app.state.settings = settings

    # Middleware order: the LAST added runs FIRST (outermost).
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,  # explicit list, never "*" with auth
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "Retry-After"],
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        # Nginx denies /metrics from the internet; Prometheus scrapes API instances directly
        # over the private network.
        pool = getattr(request.app.state, "engine", None)
        if pool is not None:
            DB_POOL_CHECKED_OUT.set(pool.pool.checkedout())
            DB_POOL_SIZE.set(pool.pool.checkedout() + pool.pool.checkedin())
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


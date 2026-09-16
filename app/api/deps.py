"""Dependency injection wiring.

Routes declare *what* they need (`service: TaskService = Depends(get_task_service)`);
this module decides *how* it is built. Tests swap any piece with
`app.dependency_overrides[...]` — no monkey-patching, no real PostgreSQL required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request, Response
from fastapi.security import OAuth2PasswordBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import session_scope
from app.core.exceptions import RateLimitedError
from app.core.metrics import RATE_LIMITED
from app.core.security import decode_access_token
from app.models.user import User
from app.repositories.task_repository import TaskRepository
from app.services.auth_service import AuthService
from app.services.cache import TaskCache
from app.services.rate_limiter import RateLimiter
from app.services.task_publisher import TaskPublisher
from app.services.task_service import TaskService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def get_app_settings(request: Request) -> Settings:
    # The Settings instance the app was built with (not a fresh read of the environment),
    # so tests can build apps with custom settings side by side.
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in session_scope(request.app.state.session_factory):
        yield session


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def get_publisher(request: Request) -> TaskPublisher:
    return request.app.state.publisher


SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]


def get_auth_service(session: SessionDep, settings: SettingsDep) -> AuthService:
    return AuthService(session, settings)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    settings: SettingsDep,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> User:
    user_id = decode_access_token(token, settings)
    return await auth.get_active_user(user_id)


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_task_service(
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    publisher: Annotated[TaskPublisher, Depends(get_publisher)],
) -> TaskService:
    cache = TaskCache(
        redis,
        ttl_seconds=settings.cache_task_ttl_seconds,
        jitter_seconds=settings.cache_ttl_jitter_seconds,
    )
    return TaskService(TaskRepository(session), cache, publisher)


async def rate_limit(
    request: Request, response: Response, redis: RedisDep, settings: SettingsDep
) -> None:
    """Per-client limit. Identity = client IP (as reported by Nginx via X-Forwarded-For,
    which uvicorn trusts only from FORWARDED_ALLOW_IPS)."""
    if not settings.rate_limit_enabled:
        return
    limiter = RateLimiter(
        redis, limit=settings.rate_limit_requests, window_seconds=settings.rate_limit_window_seconds
    )
    client_ip = request.client.host if request.client else "unknown"
    decision = await limiter.hit(f"ip:{client_ip}")
    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after_seconds),
    }
    response.headers.update(headers)
    if not decision.allowed:
        RATE_LIMITED.inc()
        raise RateLimitedError(
            "Too many requests",
            headers={**headers, "Retry-After": str(decision.reset_after_seconds)},
        )


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]

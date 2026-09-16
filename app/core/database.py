"""PostgreSQL engine and session management (SQLAlchemy 2.0 async + asyncpg).

One engine == one connection pool per process. Creating a TCP connection plus TLS/auth
handshake to PostgreSQL costs milliseconds; a pool keeps connections open and lends them
to requests, so the per-request cost is close to zero.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        # Validates a connection before use: survives PostgreSQL restarts transparently.
        pool_pre_ping=True,
        connect_args={
            "timeout": 5,  # TCP connect timeout (seconds)
            "server_settings": {
                "application_name": f"{settings.app_name}:{settings.instance_id}",
                # A runaway query must never hold a pooled connection forever.
                "statement_timeout": str(settings.db_statement_timeout_ms),
            },
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit (we serialize them after).
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Unit of work: commit on success, roll back on any exception."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

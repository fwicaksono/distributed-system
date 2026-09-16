from __future__ import annotations

import os

import pytest
from fakeredis import FakeAsyncRedis

from app.core.config import Settings

TEST_SECRET = "test-secret-key-that-is-at-least-32-characters-long"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "jwt_secret_key": TEST_SECRET,
        "log_json": False,
        "rate_limit_enabled": False,
        "postgres_host": os.getenv("POSTGRES_HOST", "localhost"),
        "postgres_password": os.getenv("POSTGRES_PASSWORD", "taskplatform"),
        "redis_host": os.getenv("REDIS_HOST", "localhost"),
        "rabbitmq_host": os.getenv("RABBITMQ_HOST", "localhost"),
        "rabbitmq_password": os.getenv("RABBITMQ_PASSWORD", "taskplatform"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()

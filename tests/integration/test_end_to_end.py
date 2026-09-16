"""End-to-end: real PostgreSQL + Redis + RabbitMQ, API and worker running in-process.

Run locally:   docker compose up -d postgres redis rabbitmq && alembic upgrade head
               RUN_INTEGRATION=1 pytest -m integration
In CI these services are GitHub Actions service containers (see .github/workflows).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from httpx import ASGITransport, AsyncClient

from app.core import rabbitmq
from app.core.database import create_engine, create_session_factory
from app.core.redis import create_redis
from app.main import create_app
from app.workers.consumer import TaskConsumer
from tests.conftest import make_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("RUN_INTEGRATION"), reason="set RUN_INTEGRATION=1"),
]


@pytest.fixture
async def running_worker():
    settings = make_settings(instance_id="it-worker")
    engine = create_engine(settings)
    redis = create_redis(settings)
    connection = await rabbitmq.connect(settings)
    channel = await connection.channel(publisher_confirms=True)
    await channel.set_qos(prefetch_count=4)
    await rabbitmq.declare_topology(channel)
    queue = await channel.get_queue(rabbitmq.PROCESS_QUEUE)
    consumer = TaskConsumer(
        settings, create_session_factory(engine), redis,
        await channel.get_exchange(rabbitmq.RETRY_EXCHANGE), ThreadPoolExecutor(2),  # type: ignore[arg-type]
    )
    tag = await queue.consume(consumer.handle)
    yield
    await queue.cancel(tag)
    await connection.close()
    await redis.aclose()
    await engine.dispose()


async def test_task_is_processed_asynchronously(running_worker) -> None:
    app = create_app(make_settings(instance_id="it-api"))
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        ready = await client.get("/health/ready")
        assert ready.status_code == 200, ready.text

        email = f"it-{uuid.uuid4().hex[:8]}@example.com"
        password = "integration-password"
        assert (await client.post("/api/v1/auth/register",
                                  json={"email": email, "password": password})).status_code == 201
        token = (await client.post("/api/v1/auth/token",
                                   data={"username": email, "password": password})).json()
        headers = {"Authorization": f"Bearer {token['access_token']}"}

        task = (await client.post(
            "/api/v1/tasks", headers=headers,
            json={"title": "it", "payload": {"kind": "sleep", "duration_seconds": 0.2}},
        )).json()
        accepted = await client.post(f"/api/v1/tasks/{task['id']}/process", headers=headers)
        assert accepted.status_code == 202

        for _ in range(50):
            status = (await client.get(f"/api/v1/tasks/{task['id']}/status",
                                       headers=headers)).json()
            if status["status"] == "completed":
                break
            await asyncio.sleep(0.2)
        assert status["status"] == "completed"
        assert status["worker_id"] == "it-worker"
        assert status["attempts"] == 1

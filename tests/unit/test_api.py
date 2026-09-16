"""HTTP-layer tests. Dependencies are overridden, so no PostgreSQL/Redis/RabbitMQ is needed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user, get_redis, get_task_service
from app.main import create_app
from app.services.cache import TaskCache
from app.services.task_service import TaskService
from tests.conftest import make_settings
from tests.unit.test_task_service import FakePublisher, FakeRepository


@pytest.fixture
def owner():
    return type("U", (), {"id": uuid.uuid4(), "email": "a@example.com", "is_active": True,
                          "created_at": datetime.now(UTC)})()


@pytest.fixture
async def client(redis, owner):
    app = create_app(make_settings())
    repo = FakeRepository()
    service = TaskService(repo, TaskCache(redis, ttl_seconds=60, jitter_seconds=0),
                          FakePublisher())  # type: ignore[arg-type]
    app.dependency_overrides[get_task_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[get_redis] = lambda: redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_liveness(client) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_request_id_is_propagated(client) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "abc12345-from-nginx"})
    assert response.headers["x-request-id"] == "abc12345-from-nginx"


async def test_request_id_generated_when_missing_or_invalid(client) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "bad id\n"})
    assert len(response.headers["x-request-id"]) == 32


async def test_create_get_process_delete_flow(client) -> None:
    created = await client.post("/api/v1/tasks", json={"title": "Generate report"})
    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "pending"
    assert created.headers["location"].endswith(f"/api/v1/tasks/{task['id']}")

    fetched = await client.get(f"/api/v1/tasks/{task['id']}")
    assert fetched.status_code == 200

    accepted = await client.post(f"/api/v1/tasks/{task['id']}/process")
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"

    again = await client.post(f"/api/v1/tasks/{task['id']}/process")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "conflict"

    deleted = await client.delete(f"/api/v1/tasks/{task['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/tasks/{task['id']}")).status_code == 404


async def test_validation_error_shape(client) -> None:
    response = await client.post(
        "/api/v1/tasks", json={"title": "", "priority": 99, "unexpected": True}
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert {tuple(d["loc"]) for d in body["details"]} >= {
        ("body", "title"), ("body", "priority"), ("body", "unexpected"),
    }


async def test_invalid_uuid_is_422(client) -> None:
    assert (await client.get("/api/v1/tasks/not-a-uuid")).status_code == 422


async def test_requires_authentication(redis) -> None:
    app = create_app(make_settings())
    app.dependency_overrides[get_redis] = lambda: redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/api/v1/tasks")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_rate_limit_returns_429(redis, owner) -> None:
    app = create_app(make_settings(rate_limit_enabled=True, rate_limit_requests=2))
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_task_service] = lambda: TaskService(
        FakeRepository(), TaskCache(redis, ttl_seconds=1, jitter_seconds=0), FakePublisher()
    )  # type: ignore[arg-type]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        codes = [(await c.post("/api/v1/tasks", json={"title": "x"})).status_code
                 for _ in range(3)]
        last = await c.post("/api/v1/tasks", json={"title": "x"})
    assert codes == [201, 201, 429]
    assert "retry-after" in last.headers


async def test_metrics_endpoint_uses_route_templates(client) -> None:
    await client.get(f"/api/v1/tasks/{uuid.uuid4()}")
    body = (await client.get("/metrics")).text
    assert 'route="/api/v1/tasks/{task_id}"' in body

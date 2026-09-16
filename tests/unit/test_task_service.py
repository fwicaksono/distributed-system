from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import ConflictError, DependencyUnavailableError, NotFoundError
from app.models.task import TaskStatus
from app.schemas.task import TaskCreate, TaskMessage
from app.services.cache import TaskCache
from app.services.task_service import (
    InvalidCursorError,
    TaskService,
    decode_cursor,
    encode_cursor,
)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeRepository:
    """In-memory stand-in for TaskRepository — the service can't tell the difference."""

    def __init__(self) -> None:
        self.session = FakeSession()
        self.rows: dict[uuid.UUID, SimpleNamespace] = {}

    async def add(self, **fields: Any) -> SimpleNamespace:
        now = datetime.now(UTC)
        row = SimpleNamespace(id=uuid.uuid4(), status=TaskStatus.PENDING, attempts=0,
                              last_error=None, created_at=now, updated_at=now, **fields)
        self.rows[row.id] = row
        return row

    async def get(self, task_id: uuid.UUID) -> SimpleNamespace | None:
        return self.rows.get(task_id)

    async def get_for_owner(self, task_id: uuid.UUID, owner_id: uuid.UUID):
        row = self.rows.get(task_id)
        return row if row and row.owner_id == owner_id else None

    async def mark_queued(self, task_id: uuid.UUID, owner_id: uuid.UUID):
        row = await self.get_for_owner(task_id, owner_id)
        if row and row.status in (TaskStatus.PENDING, TaskStatus.FAILED):
            row.status = TaskStatus.QUEUED
            return row
        return None

    async def revert_queued(self, task_id: uuid.UUID, error: str) -> None:
        self.rows[task_id].status = TaskStatus.FAILED
        self.rows[task_id].last_error = error

    async def delete_for_owner(self, task_id: uuid.UUID, owner_id: uuid.UUID) -> bool:
        if await self.get_for_owner(task_id, owner_id):
            del self.rows[task_id]
            return True
        return False


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[TaskMessage] = []

    async def publish(self, message: TaskMessage, *, priority: int = 5) -> None:
        if self.fail:
            raise DependencyUnavailableError("broker down")
        self.published.append(message)


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository()


def build_service(repo: FakeRepository, redis, publisher: FakePublisher) -> TaskService:
    cache = TaskCache(redis, ttl_seconds=60, jitter_seconds=0)
    return TaskService(repo, cache, publisher)  # type: ignore[arg-type]


async def test_submit_publishes_message_and_queues(repo, redis) -> None:
    publisher = FakePublisher()
    service = build_service(repo, redis, publisher)
    owner = uuid.uuid4()
    task = await service.create(owner, TaskCreate(title="report"))

    queued = await service.submit_for_processing(owner, task.id)

    assert queued.status == TaskStatus.QUEUED
    assert [m.task_id for m in publisher.published] == [task.id]
    assert repo.session.commits == 1  # committed BEFORE publishing


async def test_submit_twice_conflicts(repo, redis) -> None:
    service = build_service(repo, redis, FakePublisher())
    owner = uuid.uuid4()
    task = await service.create(owner, TaskCreate(title="report"))
    await service.submit_for_processing(owner, task.id)

    with pytest.raises(ConflictError):
        await service.submit_for_processing(owner, task.id)


async def test_publish_failure_compensates(repo, redis) -> None:
    service = build_service(repo, redis, FakePublisher(fail=True))
    owner = uuid.uuid4()
    task = await service.create(owner, TaskCreate(title="report"))

    with pytest.raises(DependencyUnavailableError):
        await service.submit_for_processing(owner, task.id)

    assert repo.rows[task.id].status == TaskStatus.FAILED  # client may retry


async def test_other_users_task_is_not_found(repo, redis) -> None:
    service = build_service(repo, redis, FakePublisher())
    task = await service.create(uuid.uuid4(), TaskCreate(title="secret"))

    with pytest.raises(NotFoundError):
        await service.get(uuid.uuid4(), task.id)
    with pytest.raises(NotFoundError):
        await service.submit_for_processing(uuid.uuid4(), task.id)


async def test_delete_invalidates_cache(repo, redis) -> None:
    service = build_service(repo, redis, FakePublisher())
    owner = uuid.uuid4()
    task = await service.create(owner, TaskCreate(title="t"))
    await service.get(owner, task.id)  # populate cache
    await service.delete(owner, task.id)

    with pytest.raises(NotFoundError):
        await service.get(owner, task.id)


def test_cursor_roundtrip() -> None:
    created_at, task_id = datetime.now(UTC), uuid.uuid4()
    assert decode_cursor(encode_cursor(created_at, task_id)) == (created_at, task_id)


def test_malformed_cursor_rejected() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor("not-a-cursor")

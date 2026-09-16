"""Task use cases. Orchestrates repository (PostgreSQL), cache (Redis) and publisher (RabbitMQ).

The service is the only layer that knows *all three* exist. Routes don't know about Redis;
repositories don't know about RabbitMQ.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import datetime

from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.logging import request_id_ctx
from app.models.task import SUBMITTABLE_STATUSES, TaskStatus
from app.repositories.task_repository import TaskRepository
from app.schemas.task import TaskCreate, TaskList, TaskMessage, TaskRead, TaskStatusRead
from app.services.cache import TaskCache
from app.services.task_publisher import TaskPublisher

logger = logging.getLogger(__name__)


class InvalidCursorError(AppError):
    status_code = 400
    code = "invalid_cursor"


def encode_cursor(created_at: datetime, task_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{task_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created_at, task_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(created_at), uuid.UUID(task_id)
    except (ValueError, binascii.Error) as exc:
        raise InvalidCursorError("Malformed pagination cursor") from exc


class TaskService:
    def __init__(
        self, repository: TaskRepository, cache: TaskCache, publisher: TaskPublisher
    ) -> None:
        self.repository = repository
        self.cache = cache
        self.publisher = publisher

    async def create(self, owner_id: uuid.UUID, data: TaskCreate) -> TaskRead:
        task = await self.repository.add(
            owner_id=owner_id,
            title=data.title,
            description=data.description,
            priority=data.priority,
            payload=data.payload.model_dump(),
        )
        return TaskRead.model_validate(task)

    async def get(self, owner_id: uuid.UUID, task_id: uuid.UUID) -> TaskRead:
        async def load() -> TaskRead | None:
            # Loaded without the owner filter so the cached entry is user-independent;
            # ownership is checked below on every read (cached or not).
            task = await self.repository.get(task_id)
            return TaskRead.model_validate(task) if task else None

        task = await self.cache.get_or_load(task_id, load)
        # Same 404 for "doesn't exist" and "not yours": don't leak which IDs exist.
        if task is None or task.owner_id != owner_id:
            raise NotFoundError("Task not found")
        return task

    async def list(
        self,
        owner_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None,
        status: TaskStatus | None,
    ) -> TaskList:
        after = decode_cursor(cursor) if cursor else None
        # Fetch one extra row to know whether another page exists.
        rows = await self.repository.list_for_owner(
            owner_id, limit=limit + 1, after=after, status=status
        )
        items = [TaskRead.model_validate(row) for row in rows[:limit]]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > limit else None
        )
        return TaskList(items=items, next_cursor=next_cursor)

    async def get_status(self, owner_id: uuid.UUID, task_id: uuid.UUID) -> TaskStatusRead:
        # Not cached on purpose: status changes every few seconds while processing, and
        # clients poll it precisely to see fresh state. Primary-key lookups are cheap.
        task = await self.repository.get_for_owner(task_id, owner_id)
        if task is None:
            raise NotFoundError("Task not found")
        result = (
            await self.repository.get_result(task_id)
            if task.status == TaskStatus.COMPLETED
            else None
        )
        return TaskStatusRead(
            id=task.id,
            status=task.status,
            attempts=task.attempts,
            last_error=task.last_error,
            updated_at=task.updated_at,
            result=result.output if result else None,
            duration_ms=result.duration_ms if result else None,
            worker_id=result.worker_id if result else None,
        )

    async def submit_for_processing(self, owner_id: uuid.UUID, task_id: uuid.UUID) -> TaskRead:
        """Transition to QUEUED in PostgreSQL, commit, then publish to RabbitMQ.

        Dual-write problem: DB and broker cannot share one transaction. We commit first
        (so a worker never receives a message for a row it can't see), then publish. If
        publishing fails, we compensate by marking the task FAILED so the client can retry.
        If the process crashes *between* commit and publish, the task stays QUEUED with no
        message — the transactional outbox pattern closes that gap (see README: Limitations).
        """
        task = await self.repository.mark_queued(task_id, owner_id)
        if task is None:
            existing = await self.repository.get_for_owner(task_id, owner_id)
            if existing is None:
                raise NotFoundError("Task not found")
            raise ConflictError(
                f"Task is '{existing.status.value}'; only "
                f"{[s.value for s in SUBMITTABLE_STATUSES]} tasks can be processed"
            )
        await self.repository.session.commit()
        await self.cache.invalidate(task_id)

        try:
            await self.publisher.publish(
                TaskMessage(task_id=task_id, request_id=request_id_ctx.get()),
                priority=task.priority,
            )
        except Exception:
            await self.repository.revert_queued(task_id, "enqueue failed: broker unavailable")
            await self.repository.session.commit()
            await self.cache.invalidate(task_id)
            raise
        logger.info("task queued", extra={"task_id": str(task_id)})
        return TaskRead.model_validate(task)

    async def delete(self, owner_id: uuid.UUID, task_id: uuid.UUID) -> None:
        deleted = await self.repository.delete_for_owner(task_id, owner_id)
        if not deleted:
            raise NotFoundError("Task not found")
        await self.repository.session.commit()
        # Invalidate AFTER commit: invalidating before would let a concurrent reader
        # re-populate the cache with the not-yet-deleted row.
        await self.cache.invalidate(task_id)

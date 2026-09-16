"""Data access for tasks, results and logs."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import SUBMITTABLE_STATUSES, Task, TaskLog, TaskResult, TaskStatus


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- API side ------------------------------------------------------------------------
    async def add(self, **fields: Any) -> Task:
        task = Task(**fields)
        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)  # load server defaults (status, timestamps)
        return task

    async def get_for_owner(self, task_id: uuid.UUID, owner_id: uuid.UUID) -> Task | None:
        result = await self.session.execute(
            select(Task).where(Task.id == task_id, Task.owner_id == owner_id)
        )
        return result.scalar_one_or_none()

    async def list_for_owner(
        self,
        owner_id: uuid.UUID,
        *,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None = None,
        status: TaskStatus | None = None,
    ) -> Sequence[Task]:
        """Keyset pagination: `WHERE (created_at, id) < (:c, :id)` stays O(log n) on page 1000,
        whereas `OFFSET 20000` makes PostgreSQL read and discard 20 000 rows."""
        stmt = select(Task).where(Task.owner_id == owner_id)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if after is not None:
            created_at, task_id = after
            stmt = stmt.where(
                tuple_(Task.created_at, Task.id) < tuple_(literal(created_at), literal(task_id))
            )
        stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def delete_for_owner(self, task_id: uuid.UUID, owner_id: uuid.UUID) -> bool:
        result = await self.session.execute(
            delete(Task).where(Task.id == task_id, Task.owner_id == owner_id)
        )
        return (result.rowcount or 0) > 0

    async def mark_queued(self, task_id: uuid.UUID, owner_id: uuid.UUID) -> Task | None:
        """Atomic compare-and-set. Two concurrent POST /process calls on different API servers
        race here; PostgreSQL row locking guarantees only one UPDATE matches."""
        result = await self.session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.owner_id == owner_id,
                Task.status.in_(SUBMITTABLE_STATUSES),
            )
            .values(status=TaskStatus.QUEUED, last_error=None, attempts=0)
            .returning(Task)
        )
        return result.scalar_one_or_none()

    async def revert_queued(self, task_id: uuid.UUID, error: str) -> None:
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.QUEUED)
            .values(status=TaskStatus.FAILED, last_error=error)
        )

    async def get_result(self, task_id: uuid.UUID) -> TaskResult | None:
        result = await self.session.execute(
            select(TaskResult).where(TaskResult.task_id == task_id)
        )
        return result.scalar_one_or_none()

    # --- Worker side ---------------------------------------------------------------------
    async def claim(self, task_id: uuid.UUID, lease_seconds: int) -> Task | None:
        """Claim a task for processing (idempotency guard).

        Succeeds when the task is queued, or when a previous worker's lease expired (that
        worker probably crashed). Returns None when another worker holds a valid lease or
        the task is already finished — the message is then a duplicate.
        """
        now = func.now()
        result = await self.session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                or_(
                    Task.status == TaskStatus.QUEUED,
                    and_(Task.status == TaskStatus.PROCESSING, Task.lease_expires_at < now),
                ),
            )
            .values(
                status=TaskStatus.PROCESSING,
                attempts=Task.attempts + 1,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            .returning(Task)
        )
        return result.scalar_one_or_none()

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return await self.session.get(Task, task_id)

    async def complete(
        self, task_id: uuid.UUID, *, output: dict[str, Any], duration_ms: int, worker_id: str
    ) -> None:
        # ON CONFLICT DO NOTHING: completing twice is harmless (idempotent write).
        await self.session.execute(
            insert(TaskResult)
            .values(task_id=task_id, output=output, duration_ms=duration_ms, worker_id=worker_id)
            .on_conflict_do_nothing(index_elements=[TaskResult.task_id])
        )
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status=TaskStatus.COMPLETED, lease_expires_at=None, last_error=None)
        )

    async def release_for_retry(self, task_id: uuid.UUID, error: str) -> None:
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.PROCESSING)
            .values(status=TaskStatus.QUEUED, lease_expires_at=None, last_error=error)
        )

    async def fail(self, task_id: uuid.UUID, error: str) -> None:
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status != TaskStatus.COMPLETED)
            .values(status=TaskStatus.FAILED, lease_expires_at=None, last_error=error)
        )

    async def add_log(self, task_id: uuid.UUID, level: str, message: str, source: str) -> None:
        self.session.add(TaskLog(task_id=task_id, level=level, message=message, source=source))

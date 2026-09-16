from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.task import TaskStatus


class TaskPayload(BaseModel):
    """What the worker should do. Deliberately trivial: the architecture is the point.

    kind="sleep": I/O-bound simulation (asyncio.sleep) — many run concurrently on one core.
    kind="hash":  CPU-bound work (repeated SHA-256) — runs in a process pool for parallelism.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["sleep", "hash"] = "sleep"
    duration_seconds: float = Field(default=2.0, ge=0, le=30)
    iterations: int = Field(default=200_000, ge=1, le=5_000_000)
    # Probability (0-1) that processing raises a transient error: exercises retry + DLQ.
    failure_rate: float = Field(default=0.0, ge=0, le=1)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    priority: int = Field(default=5, ge=0, le=10)
    payload: TaskPayload = Field(default_factory=TaskPayload)


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    description: str | None
    priority: int
    payload: dict[str, Any]
    status: TaskStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class TaskStatusRead(BaseModel):
    id: uuid.UUID
    status: TaskStatus
    attempts: int
    last_error: str | None
    updated_at: datetime
    result: dict[str, Any] | None = None
    duration_ms: int | None = None
    worker_id: str | None = None


class TaskList(BaseModel):
    items: list[TaskRead]
    next_cursor: str | None = Field(
        description="Opaque cursor for the next page; null when there are no more results."
    )


class TaskAccepted(BaseModel):
    id: uuid.UUID
    status: TaskStatus
    status_url: str


class TaskMessage(BaseModel):
    """Message contract between API (producer) and worker (consumer). Version it explicitly."""

    version: Literal[1] = 1
    task_id: uuid.UUID
    request_id: str | None = None

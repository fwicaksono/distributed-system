from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import CurrentUser, TaskServiceDep, rate_limit
from app.models.task import TaskStatus
from app.schemas.common import ErrorResponse
from app.schemas.task import TaskAccepted, TaskCreate, TaskList, TaskRead, TaskStatusRead

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    dependencies=[Depends(rate_limit)],
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    data: TaskCreate, user: CurrentUser, service: TaskServiceDep, request: Request,
    response: Response,
) -> TaskRead:
    task = await service.create(user.id, data)
    response.headers["Location"] = str(request.url_for("get_task", task_id=task.id))
    return task


@router.get("", response_model=TaskList)
async def list_tasks(
    user: CurrentUser,
    service: TaskServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
) -> TaskList:
    return await service.list(user.id, limit=limit, cursor=cursor, status=status_filter)


@router.get("/{task_id}", response_model=TaskRead, name="get_task")
async def get_task(task_id: uuid.UUID, user: CurrentUser, service: TaskServiceDep) -> TaskRead:
    return await service.get(user.id, task_id)


@router.post(
    "/{task_id}/process",
    response_model=TaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def process_task(
    task_id: uuid.UUID, user: CurrentUser, service: TaskServiceDep, request: Request,
    response: Response,
) -> TaskAccepted:
    """202 Accepted = "I have durably queued your work", NOT "your work is done".
    The client polls the status URL (or, in a bigger system, receives a webhook)."""
    task = await service.submit_for_processing(user.id, task_id)
    status_url = str(request.url_for("get_task_status", task_id=task.id))
    response.headers["Location"] = status_url
    return TaskAccepted(id=task.id, status=task.status, status_url=status_url)


@router.get("/{task_id}/status", response_model=TaskStatusRead, name="get_task_status")
async def get_task_status(
    task_id: uuid.UUID, user: CurrentUser, service: TaskServiceDep
) -> TaskStatusRead:
    return await service.get_status(user.id, task_id)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: uuid.UUID, user: CurrentUser, service: TaskServiceDep) -> Response:
    await service.delete(user.id, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

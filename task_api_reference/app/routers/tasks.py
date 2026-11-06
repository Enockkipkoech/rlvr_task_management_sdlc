"""PATCH /tasks/{id}."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends

from app.db import get_conn
from app.deps import get_current_user
from app.schemas import TaskOut, TaskUpdate
from app.services import project_service

router = APIRouter(tags=["tasks"])


@router.patch("/tasks/{task_id}", response_model=TaskOut)
def update_task(
    task_id: UUID,
    body: TaskUpdate,
    user: dict[str, Any] = Depends(get_current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    return project_service.update_task(conn, user, task_id, body.changes())

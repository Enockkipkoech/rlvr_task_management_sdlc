"""Project routes and the project-scoped task routes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Response

from app.db import get_conn
from app.deps import get_current_user
from app.schemas import ProjectCreate, ProjectOut, TaskCreate, TaskOut
from app.services import project_service

router = APIRouter(tags=["projects"])


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(
    body: ProjectCreate,
    user: dict[str, Any] = Depends(get_current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    return project_service.create_project(conn, user, body.name)


@router.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
def create_task(
    project_id: UUID,
    body: TaskCreate,
    user: dict[str, Any] = Depends(get_current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    return project_service.create_task(conn, user, project_id, body.model_dump())


@router.get("/projects/{project_id}/tasks", response_model=list[TaskOut])
def list_tasks(
    project_id: UUID,
    user: dict[str, Any] = Depends(get_current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    return project_service.list_tasks(conn, user, project_id)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(
    project_id: UUID,
    user: dict[str, Any] = Depends(get_current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    project_service.delete_project(conn, user, project_id)
    return Response(status_code=204)

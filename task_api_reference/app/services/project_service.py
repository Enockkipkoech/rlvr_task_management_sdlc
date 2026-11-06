"""Projects and tasks: authorization, business rules, transactions.

Policy
- owner (and admin): full control of the project and its tasks.
- assignee of a task: may read their own tasks in the project and change only
  the ``status`` of a task assigned to them.
- anyone else: 403.  Unknown ids: 404.
- A project with an in_progress task cannot be deleted (409).  The check and
  the delete happen in one transaction under a row lock on the project; task
  writers take the conflicting shared lock, so the two cannot interleave.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from app.errors import AppError
from app.repositories import projects, tasks, users

Row = dict[str, Any]


def _forbidden(message: str = "You do not have access to this resource") -> AppError:
    return AppError(403, "forbidden", message)


def _is_privileged(user: Row, project: Row) -> bool:
    return user["role"] == "admin" or project["owner_id"] == user["id"]


def _require_project(conn: psycopg.Connection, project_id: UUID) -> Row:
    project = projects.get(conn, project_id)
    if project is None:
        raise AppError(404, "project_not_found", "Project not found")
    return project


def _require_assignee_exists(conn: psycopg.Connection, assignee_id: UUID | None) -> None:
    if assignee_id is not None and users.get_by_id(conn, assignee_id) is None:
        raise AppError(422, "assignee_not_found", "Assignee does not exist")


def create_project(conn: psycopg.Connection, user: Row, name: str) -> Row:
    """Create a project owned by ``user``."""
    with conn.transaction():
        return projects.create(conn, name=name, owner_id=user["id"])


def create_task(conn: psycopg.Connection, user: Row, project_id: UUID, data: dict[str, Any]) -> Row:
    """Create a task in a project.  Owner/admin only."""
    project = _require_project(conn, project_id)
    if not _is_privileged(user, project):
        raise _forbidden()
    _require_assignee_exists(conn, data.get("assignee_id"))
    try:
        with conn.transaction():
            if projects.lock(conn, project_id, exclusive=False) is None:
                raise AppError(404, "project_not_found", "Project not found")
            return tasks.create(
                conn,
                project_id=project_id,
                title=data["title"],
                description=data.get("description", ""),
                status=data.get("status", "todo"),
                assignee_id=data.get("assignee_id"),
            )
    except psycopg.errors.ForeignKeyViolation:
        raise AppError(422, "assignee_not_found", "Assignee does not exist") from None


def list_tasks(conn: psycopg.Connection, user: Row, project_id: UUID) -> list[Row]:
    """Owner/admin: every task.  Assignee: only the tasks assigned to them."""
    project = _require_project(conn, project_id)
    if _is_privileged(user, project):
        return tasks.list_for_project(conn, project_id)
    if tasks.user_has_task_in_project(conn, project_id, user["id"]):
        return tasks.list_for_project(conn, project_id, assignee_id=user["id"])
    raise _forbidden()


def update_task(conn: psycopg.Connection, user: Row, task_id: UUID, changes: dict[str, Any]) -> Row:
    """Update a task.  Owner/admin: any field.  Assignee: ``status`` only."""
    task = tasks.get(conn, task_id)
    if task is None:
        raise AppError(404, "task_not_found", "Task not found")
    try:
        with conn.transaction():
            project = projects.lock(conn, task["project_id"], exclusive=False)
            if project is None:
                raise AppError(404, "task_not_found", "Task not found")
            task = tasks.get(conn, task_id)
            if task is None:
                raise AppError(404, "task_not_found", "Task not found")
            if not _is_privileged(user, project):
                if task["assignee_id"] != user["id"]:
                    raise _forbidden()
                if set(changes) - {"status"}:
                    raise _forbidden("Assignees may only change the task status")
            if "assignee_id" in changes:
                _require_assignee_exists(conn, changes["assignee_id"])
            updated = tasks.update(conn, task_id, changes)
            if updated is None:
                raise AppError(404, "task_not_found", "Task not found")
            return updated
    except psycopg.errors.ForeignKeyViolation:
        raise AppError(422, "assignee_not_found", "Assignee does not exist") from None


def delete_project(conn: psycopg.Connection, user: Row, project_id: UUID) -> None:
    """Delete a project and its tasks.  Owner/admin only; 409 if any task is in_progress."""
    project = _require_project(conn, project_id)
    if not _is_privileged(user, project):
        raise _forbidden()
    with conn.transaction():
        if projects.lock(conn, project_id, exclusive=True) is None:
            raise AppError(404, "project_not_found", "Project not found")
        if projects.has_in_progress_tasks(conn, project_id):
            raise AppError(409, "project_has_in_progress_tasks",
                           "Project has in-progress tasks and cannot be deleted")
        projects.delete(conn, project_id)

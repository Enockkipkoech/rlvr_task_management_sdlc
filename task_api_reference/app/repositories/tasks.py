"""Task persistence.  Every query is parameterized."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql

_COLUMNS = "id, project_id, assignee_id, title, description, status, created_at, updated_at"
_UPDATABLE = ("title", "description", "status", "assignee_id")


def create(
    conn: psycopg.Connection,
    *,
    project_id: UUID | str,
    title: str,
    description: str,
    status: str,
    assignee_id: UUID | str | None,
) -> dict[str, Any]:
    """Insert a task and return the row."""
    return conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, assignee_id) "
        f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
        (str(project_id), title, description, status,
         str(assignee_id) if assignee_id is not None else None),
    ).fetchone()


def get(conn: psycopg.Connection, task_id: UUID | str) -> dict[str, Any] | None:
    """Return the task, or None."""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM tasks WHERE id = %s", (str(task_id),)
    ).fetchone()


def list_for_project(
    conn: psycopg.Connection,
    project_id: UUID | str,
    *,
    assignee_id: UUID | str | None = None,
) -> list[dict[str, Any]]:
    """Tasks of a project ordered by creation time; optionally only one assignee's."""
    if assignee_id is None:
        return conn.execute(
            f"SELECT {_COLUMNS} FROM tasks WHERE project_id = %s ORDER BY created_at, id",
            (str(project_id),),
        ).fetchall()
    return conn.execute(
        f"SELECT {_COLUMNS} FROM tasks WHERE project_id = %s AND assignee_id = %s "
        "ORDER BY created_at, id",
        (str(project_id), str(assignee_id)),
    ).fetchall()


def update(
    conn: psycopg.Connection, task_id: UUID | str, changes: dict[str, Any]
) -> dict[str, Any] | None:
    """Apply ``changes`` (whitelisted column names only) and return the row."""
    unknown = set(changes) - set(_UPDATABLE)
    if unknown:
        raise ValueError(f"cannot update columns: {sorted(unknown)}")
    if not changes:
        return get(conn, task_id)
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(name)) for name in changes
    )
    values = [str(v) if name == "assignee_id" and v is not None else v
              for name, v in changes.items()]
    query = sql.SQL("UPDATE tasks SET {}, updated_at = now() WHERE id = %s RETURNING {}").format(
        assignments, sql.SQL(_COLUMNS)
    )
    return conn.execute(query, (*values, str(task_id))).fetchone()


def user_has_task_in_project(
    conn: psycopg.Connection, project_id: UUID | str, user_id: UUID | str
) -> bool:
    """True when the user is assignee of at least one task in the project."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM tasks WHERE project_id = %s AND assignee_id = %s) AS found",
        (str(project_id), str(user_id)),
    ).fetchone()
    return bool(row["found"])

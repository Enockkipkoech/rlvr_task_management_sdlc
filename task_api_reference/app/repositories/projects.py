"""Project persistence.  Every query is parameterized."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

_COLUMNS = "id, name, owner_id, created_at"


def create(conn: psycopg.Connection, *, name: str, owner_id: UUID | str) -> dict[str, Any]:
    """Insert a project and return the row."""
    return conn.execute(
        f"INSERT INTO projects (name, owner_id) VALUES (%s, %s) RETURNING {_COLUMNS}",
        (name, str(owner_id)),
    ).fetchone()


def get(conn: psycopg.Connection, project_id: UUID | str) -> dict[str, Any] | None:
    """Return the project, or None."""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM projects WHERE id = %s", (str(project_id),)
    ).fetchone()


def lock(
    conn: psycopg.Connection, project_id: UUID | str, *, exclusive: bool
) -> dict[str, Any] | None:
    """Row-lock the project inside the caller's transaction.

    ``exclusive=True`` takes FOR UPDATE (deleters); ``exclusive=False`` takes
    FOR SHARE (task writers).  The two modes conflict, which serializes
    "delete project" against "move a task to in_progress".  Returns the row,
    or None if the project no longer exists.
    """
    mode = "FOR UPDATE" if exclusive else "FOR SHARE"
    return conn.execute(
        f"SELECT {_COLUMNS} FROM projects WHERE id = %s {mode}", (str(project_id),)
    ).fetchone()


def has_in_progress_tasks(conn: psycopg.Connection, project_id: UUID | str) -> bool:
    """True when at least one task of the project has status 'in_progress'."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM tasks WHERE project_id = %s AND status = 'in_progress') AS found",
        (str(project_id),),
    ).fetchone()
    return bool(row["found"])


def delete(conn: psycopg.Connection, project_id: UUID | str) -> bool:
    """Delete the project (tasks cascade).  True if a row was deleted."""
    cur = conn.execute("DELETE FROM projects WHERE id = %s", (str(project_id),))
    return cur.rowcount == 1

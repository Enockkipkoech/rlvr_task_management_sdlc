"""User persistence.  Every query is parameterized."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

_COLUMNS = "id, email, password_hash, role, created_at"


def create_user(
    conn: psycopg.Connection, *, email: str, password_hash: str, role: str = "user"
) -> dict[str, Any]:
    """Insert a user and return the row.

    Raises psycopg.errors.UniqueViolation when the email already exists.
    """
    return conn.execute(
        f"INSERT INTO users (email, password_hash, role) VALUES (%s, %s, %s) RETURNING {_COLUMNS}",
        (email, password_hash, role),
    ).fetchone()


def get_by_email(conn: psycopg.Connection, email: str) -> dict[str, Any] | None:
    """Return the user with this (already lower-cased) email, or None."""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM users WHERE email = %s", (email,)
    ).fetchone()


def get_by_id(conn: psycopg.Connection, user_id: UUID | str) -> dict[str, Any] | None:
    """Return the user with this id, or None."""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM users WHERE id = %s", (str(user_id),)
    ).fetchone()

"""FastAPI dependencies: authentication."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import get_conn
from app.errors import AppError
from app.repositories import users
from app.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)

_UNAUTHENTICATED = {"WWW-Authenticate": "Bearer"}


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    conn: psycopg.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Resolve the bearer token to a user row (role is read from the database)."""
    if credentials is None or not credentials.credentials:
        raise AppError(401, "not_authenticated", "Authentication required",
                       headers=_UNAUTHENTICATED)
    subject = decode_access_token(credentials.credentials)
    try:
        user_id = UUID(subject)
    except ValueError:
        raise AppError(401, "invalid_token", "Invalid or expired token",
                       headers=_UNAUTHENTICATED) from None
    user = users.get_by_id(conn, user_id)
    if user is None:
        raise AppError(401, "invalid_token", "Invalid or expired token",
                       headers=_UNAUTHENTICATED)
    return user

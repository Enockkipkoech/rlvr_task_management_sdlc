"""Registration and login."""

from __future__ import annotations

from typing import Any

import psycopg

from app.config import get_settings
from app.errors import AppError
from app.repositories import users
from app.security import create_access_token, hash_password, verify_password

# Verified against when the email is unknown so login timing does not reveal
# which emails exist.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def register_user(conn: psycopg.Connection, email: str, password: str) -> dict[str, Any]:
    """Create a 'user'-role account.  409 ``email_taken`` on duplicates."""
    password_hash = hash_password(password)
    try:
        with conn.transaction():
            return users.create_user(conn, email=email, password_hash=password_hash)
    except psycopg.errors.UniqueViolation:
        raise AppError(409, "email_taken", "Email already registered") from None


def authenticate(conn: psycopg.Connection, email: str, password: str) -> dict[str, Any]:
    """Return ``{"access_token", "token_type", "expires_in"}`` or raise 401.

    Unknown email and wrong password produce the identical 401 response.
    """
    user = users.get_by_email(conn, email)
    if user is None:
        verify_password(password, _DUMMY_HASH)
        ok = False
    else:
        ok = verify_password(password, user["password_hash"])
    if not ok:
        raise AppError(401, "invalid_credentials", "Invalid email or password",
                       headers={"WWW-Authenticate": "Bearer"})
    ttl = get_settings().access_token_ttl_minutes
    return {
        "access_token": create_access_token(str(user["id"]), ttl),
        "token_type": "bearer",
        "expires_in": ttl * 60,
    }

"""Password hashing and JWT helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings
from app.errors import AppError

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Return a salted Argon2id hash of ``plain``."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True when ``plain`` matches ``hashed``; never raise on bad input."""
    try:
        return _hasher.verify(hashed, plain)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(user_id: str, ttl_minutes: int | None = None) -> str:
    """Create a signed HS256 JWT with ``sub``, ``iat``, ``exp`` and ``jti`` claims."""
    settings = get_settings()
    ttl = settings.access_token_ttl_minutes if ttl_minutes is None else ttl_minutes
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=ttl),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str:
    """Validate ``token`` and return its subject (user id).

    Raises AppError(401, "invalid_token") for any malformed, tampered,
    expired, unsigned or wrong-algorithm token.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError:
        raise AppError(401, "invalid_token", "Invalid or expired token",
                       headers={"WWW-Authenticate": "Bearer"}) from None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AppError(401, "invalid_token", "Invalid or expired token",
                       headers={"WWW-Authenticate": "Bearer"})
    return subject

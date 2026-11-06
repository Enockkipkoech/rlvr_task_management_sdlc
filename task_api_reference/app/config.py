"""Runtime settings, read from the environment at call time."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unsafe."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    jwt_algorithm: str
    access_token_ttl_minutes: int
    password_min_length: int
    title_max_length: int


def get_settings() -> Settings:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is not set")
    jwt_secret = os.environ.get("JWT_SECRET", "")
    if len(jwt_secret) < 32:
        raise ConfigError("JWT_SECRET must be set and at least 32 characters long")
    return Settings(
        database_url=database_url,
        jwt_secret=jwt_secret,
        jwt_algorithm="HS256",
        access_token_ttl_minutes=int(os.environ.get("ACCESS_TOKEN_TTL_MINUTES", "30")),
        password_min_length=int(os.environ.get("PASSWORD_MIN_LENGTH", "8")),
        title_max_length=int(os.environ.get("TITLE_MAX_LENGTH", "120")),
    )


def password_min_length() -> int:
    return int(os.environ.get("PASSWORD_MIN_LENGTH", "8"))


def title_max_length() -> int:
    return int(os.environ.get("TITLE_MAX_LENGTH", "120"))

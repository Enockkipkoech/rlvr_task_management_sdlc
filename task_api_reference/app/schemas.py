"""Pydantic request/response schemas with validation and sanitization."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator, model_validator

from app.config import password_min_length, title_max_length

TaskStatus = Literal["todo", "in_progress", "done"]

# C0 control characters except TAB (0x09) and LF (0x0A); includes NUL, which
# PostgreSQL text columns cannot store.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean_text(value: str, *, field: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{field} contains control characters")
    return value.strip()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictModel):
    email: EmailStr
    password: str

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if len(v) < password_min_length():
            raise ValueError(f"password must be at least {password_min_length()} characters")
        if len(v) > 128:
            raise ValueError("password must be at most 128 characters")
        if "\x00" in v:
            raise ValueError("password contains NUL")
        return v


class LoginRequest(StrictModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or len(v) > 254 or "\x00" in v:
            raise ValueError("invalid email")
        return v

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if not v or len(v) > 128 or "\x00" in v:
            raise ValueError("invalid password")
        return v


class UserOut(BaseModel):
    id: UUID
    email: str
    role: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class ProjectCreate(StrictModel):
    name: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        v = _clean_text(v, field="name")
        if not 1 <= len(v) <= 100:
            raise ValueError("name must be 1-100 characters")
        return v


class ProjectOut(BaseModel):
    id: UUID
    name: str
    owner_id: UUID


class TaskCreate(StrictModel):
    title: str
    description: str = ""
    status: TaskStatus = "todo"
    assignee_id: UUID | None = None

    @field_validator("title")
    @classmethod
    def _check_title(cls, v: str) -> str:
        v = _clean_text(v, field="title")
        if not 1 <= len(v) <= title_max_length():
            raise ValueError(f"title must be 1-{title_max_length()} characters")
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        v = _clean_text(v, field="description")
        if len(v) > 5000:
            raise ValueError("description must be at most 5000 characters")
        return v


class TaskUpdate(StrictModel):
    title: str | None = None
    description: str | None = None
    status: TaskStatus | None = None
    assignee_id: UUID | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "TaskUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for name in ("title", "description", "status"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    @field_validator("title")
    @classmethod
    def _check_title(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = _clean_text(v, field="title")
        if not 1 <= len(v) <= title_max_length():
            raise ValueError(f"title must be 1-{title_max_length()} characters")
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = _clean_text(v, field="description")
        if len(v) > 5000:
            raise ValueError("description must be at most 5000 characters")
        return v

    def changes(self) -> dict[str, Any]:
        """Only the fields the client actually sent (explicit null unassigns)."""
        return {name: getattr(self, name) for name in self.model_fields_set}


class TaskOut(BaseModel):
    id: UUID
    project_id: UUID
    assignee_id: UUID | None
    title: str
    description: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime

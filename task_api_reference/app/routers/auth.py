"""POST /register and POST /login."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends

from app.db import get_conn
from app.schemas import LoginRequest, RegisterRequest, TokenOut, UserOut
from app.services import auth_service

router = APIRouter(tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
def register(body: RegisterRequest, conn: psycopg.Connection = Depends(get_conn)):
    return auth_service.register_user(conn, body.email, body.password)


@router.post("/login", response_model=TokenOut)
def login(body: LoginRequest, conn: psycopg.Connection = Depends(get_conn)):
    return auth_service.authenticate(conn, body.email, body.password)

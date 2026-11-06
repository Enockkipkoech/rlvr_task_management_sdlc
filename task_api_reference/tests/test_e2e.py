"""End-to-end tests for the Task Management API.

Uses DATABASE_URL (already migrated) when provided; otherwise creates a
throw-away database through TEST_DATABASE_ADMIN_URL.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("JWT_SECRET", "e2e-secret-" + "x" * 40)
PASSWORD = "Correct-Horse-Battery-42"


@pytest.fixture(scope="module")
def client():
    created = None
    if not os.environ.get("DATABASE_URL"):
        admin = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
        created = f"e2e_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{created}"')
        os.environ["DATABASE_URL"] = make_conninfo(admin, dbname=created)
        from app.migrate import run_migrations

        run_migrations(os.environ["DATABASE_URL"])
    from app.main import create_app

    yield TestClient(create_app())
    if created:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{created}" WITH (FORCE)')


def signup(client):
    email = f"{uuid.uuid4().hex[:10]}@example.com"
    assert client.post("/register", json={"email": email, "password": PASSWORD}).status_code == 201
    token = client.post("/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"email": email, "headers": {"Authorization": f"Bearer {token}"}}


def test_health_reports_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_reports_503_when_database_is_down(client, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=1")
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["error"]["code"] == "database_unavailable"


def test_register_login_and_duplicate(client):
    user = signup(client)
    dup = client.post("/register", json={"email": user["email"], "password": PASSWORD})
    assert dup.status_code == 409
    bad = client.post("/login", json={"email": user["email"], "password": "wrong-password-1"})
    assert bad.status_code == 401


def test_register_validation(client):
    assert client.post("/register", json={"email": "nope", "password": PASSWORD}).status_code == 422
    assert client.post("/register", json={"email": "a@b.com", "password": "x"}).status_code == 422


def test_project_and_task_lifecycle(client):
    owner = signup(client)
    pid = client.post("/projects", json={"name": "Alpha"}, headers=owner["headers"]).json()["id"]
    task = client.post(f"/projects/{pid}/tasks", json={"title": "Write tests"}, headers=owner["headers"]).json()
    assert task["status"] == "todo"
    patched = client.patch(f"/tasks/{task['id']}", json={"status": "in_progress"}, headers=owner["headers"])
    assert patched.json()["status"] == "in_progress"
    assert client.delete(f"/projects/{pid}", headers=owner["headers"]).status_code == 409
    client.patch(f"/tasks/{task['id']}", json={"status": "done"}, headers=owner["headers"])
    assert client.delete(f"/projects/{pid}", headers=owner["headers"]).status_code == 204
    assert client.get(f"/projects/{pid}/tasks", headers=owner["headers"]).status_code == 404


def test_listing_tasks(client):
    owner = signup(client)
    pid = client.post("/projects", json={"name": "L"}, headers=owner["headers"]).json()["id"]
    for i in range(3):
        client.post(f"/projects/{pid}/tasks", json={"title": f"t{i}"}, headers=owner["headers"])
    rows = client.get(f"/projects/{pid}/tasks", headers=owner["headers"]).json()
    assert [r["title"] for r in rows] == ["t0", "t1", "t2"]


def test_stranger_is_forbidden(client):
    owner, stranger = signup(client), signup(client)
    pid = client.post("/projects", json={"name": "Secret"}, headers=owner["headers"]).json()["id"]
    tid = client.post(f"/projects/{pid}/tasks", json={"title": "x"}, headers=owner["headers"]).json()["id"]
    assert client.get(f"/projects/{pid}/tasks", headers=stranger["headers"]).status_code == 403
    assert client.post(f"/projects/{pid}/tasks", json={"title": "y"}, headers=stranger["headers"]).status_code == 403
    assert client.patch(f"/tasks/{tid}", json={"status": "done"}, headers=stranger["headers"]).status_code == 403
    assert client.delete(f"/projects/{pid}", headers=stranger["headers"]).status_code == 403


def test_assignee_permissions(client):
    owner, helper = signup(client), signup(client)
    helper_id = client.post("/login", json={"email": helper["email"], "password": PASSWORD}).json()
    me = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    helper_uuid = me.execute("SELECT id FROM users WHERE email = %s", (helper["email"],)).fetchone()[0]
    me.close()
    pid = client.post("/projects", json={"name": "P"}, headers=owner["headers"]).json()["id"]
    tid = client.post(f"/projects/{pid}/tasks", json={"title": "x", "assignee_id": str(helper_uuid)},
                      headers=owner["headers"]).json()["id"]
    assert helper_id["token_type"] == "bearer"
    assert client.patch(f"/tasks/{tid}", json={"status": "done"}, headers=helper["headers"]).status_code == 200
    assert client.patch(f"/tasks/{tid}", json={"title": "z"}, headers=helper["headers"]).status_code == 403
    assert len(client.get(f"/projects/{pid}/tasks", headers=helper["headers"]).json()) == 1


def test_unauthenticated_and_bad_tokens(client):
    assert client.post("/projects", json={"name": "x"}).status_code == 401
    bad = {"Authorization": "Bearer abc.def.ghi"}
    assert client.post("/projects", json={"name": "x"}, headers=bad).status_code == 401


def test_validation_and_not_found(client):
    owner = signup(client)
    assert client.post("/projects", json={"name": ""}, headers=owner["headers"]).status_code == 422
    assert client.get("/projects/not-a-uuid/tasks", headers=owner["headers"]).status_code == 422
    assert client.delete(f"/projects/{uuid.uuid4()}", headers=owner["headers"]).status_code == 404
    assert client.get("/nope").status_code == 404


def test_sql_injection_payloads_are_inert(client):
    owner = signup(client)
    payload = "'; DROP TABLE tasks; --"
    pid = client.post("/projects", json={"name": payload}, headers=owner["headers"]).json()["id"]
    assert client.post(f"/projects/{pid}/tasks", json={"title": payload}, headers=owner["headers"]).status_code == 201
    assert client.post("/login", json={"email": "' OR '1'='1", "password": payload}).status_code in (401, 422)


def test_unexpected_errors_are_sanitised(client, monkeypatch):
    from app.repositories import users

    monkeypatch.setattr(users, "get_by_email", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail")))
    quiet = TestClient(client.app, raise_server_exceptions=False)
    r = quiet.post("/login", json={"email": "a@b.com", "password": PASSWORD})
    assert r.status_code == 500 and "secret detail" not in r.text

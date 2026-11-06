"""Small helpers shared by the grader suites."""

from __future__ import annotations

import uuid

PASSWORD = "Correct-Horse-Battery-42"


def unique_email(prefix: str = "u") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}@example.com"


def password_for(params_min: int = 8) -> str:
    return (PASSWORD * 3)[: max(params_min, len(PASSWORD))]


def register_and_login(client, email: str | None = None, password: str | None = None) -> dict:
    email = email or unique_email()
    password = password or PASSWORD
    r = client.post("/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    login = client.post("/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {
        "id": r.json()["id"],
        "email": email,
        "password": password,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def make_project(client, headers, name: str = "Project") -> dict:
    r = client.post("/projects", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def make_task(client, headers, project_id: str, title: str = "Task", **extra) -> dict:
    r = client.post(f"/projects/{project_id}/tasks", json={"title": title, **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def assert_error_shape(resp, status: int | None = None) -> dict:
    if status is not None:
        assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) >= {"code", "message"}, body
    assert isinstance(body["error"]["code"], str) and body["error"]["code"]
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    return body["error"]

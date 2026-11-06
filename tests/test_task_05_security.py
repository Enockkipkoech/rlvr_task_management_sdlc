"""Task 5 grader: validation, error handling and SQL safety.

Owned files: app/schemas.py, app/errors.py, app/repositories/users.py
"""

from __future__ import annotations

import ast
import inspect
import uuid

import pytest
from fastapi.testclient import TestClient

from helpers import PASSWORD, assert_error_shape, make_project, make_task, register_and_login, unique_email

SQLI_LOGIN = ["' OR '1'='1", "admin'--", "x'; DROP TABLE users; --", "\" OR \"\"=\"", "' UNION SELECT 1,2,3,4 --",
              "a@b.com' OR '1'='1"]
BAD_TOKENS = ["", "a.b", "a.b.c.d", "....", "eyJhbGciOiJIUzI1NiJ9.e30.", "!!!.???.***", "x" * 10000,
              "ünï.cödé.tøken", "Bearer", "null", "undefined", "0"]


def _pw(params, extra=0):
    return ("Aa1!" * 40)[: params["password_min_length"] + extra]


# ------------------------------- functional --------------------------------
@pytest.mark.functional
@pytest.mark.parametrize("email", ["not-an-email", "a@", "@b.com", "", "   ", "a b@c.com", "a@b", 12345, None, ["a@b.com"]])
def test_register_rejects_bad_email(client, params, email):
    assert_error_shape(client.post("/register", json={"email": email, "password": _pw(params)}), 422)


@pytest.mark.functional
def test_password_length_boundaries(client, params):
    low = client.post("/register", json={"email": unique_email(), "password": _pw(params, -1)})
    assert_error_shape(low, 422)
    assert client.post("/register", json={"email": unique_email(), "password": _pw(params)}).status_code == 201
    assert client.post("/register", json={"email": unique_email(), "password": "p" * 128}).status_code == 201
    assert client.post("/register", json={"email": unique_email(), "password": "p" * 129}).status_code == 422


@pytest.mark.functional
@pytest.mark.parametrize("body", [{}, {"email": "a@b.com"}, {"password": "x" * 12}, {"email": "a@b.com", "password": 123456789012}])
def test_register_rejects_missing_or_mistyped_fields(client, body):
    assert client.post("/register", json=body).status_code == 422


@pytest.mark.functional
def test_register_normalizes_email(client, params):
    raw = f"  {uuid.uuid4().hex[:8]}@Example.COM  "
    r = client.post("/register", json={"email": raw, "password": _pw(params)})
    assert r.status_code == 201 and r.json()["email"] == raw.strip().lower()


@pytest.mark.functional
@pytest.mark.parametrize("name", ["", "   ", "x" * 101, None, 5, ["a"]])
def test_project_name_validation(client, make_user, name):
    assert client.post("/projects", json={"name": name}, headers=make_user()["headers"]).status_code == 422


@pytest.mark.functional
def test_project_name_is_trimmed_and_bounded(client, make_user):
    h = make_user()["headers"]
    assert client.post("/projects", json={"name": "  Alpha  "}, headers=h).json()["name"] == "Alpha"
    assert client.post("/projects", json={"name": "n" * 100}, headers=h).status_code == 201


@pytest.mark.functional
def test_task_title_boundaries(client, make_user, params):
    h = make_user()["headers"]
    pid = make_project(client, h)["id"]
    top = params["title_max_length"]
    assert client.post(f"/projects/{pid}/tasks", json={"title": "t" * top}, headers=h).status_code == 201
    assert_error_shape(client.post(f"/projects/{pid}/tasks", json={"title": "t" * (top + 1)}, headers=h), 422)
    for bad in ("", "   ", None, 7):
        assert client.post(f"/projects/{pid}/tasks", json={"title": bad}, headers=h).status_code == 422


@pytest.mark.functional
@pytest.mark.parametrize("extra", [{"status": "blocked"}, {"status": "DONE"}, {"status": 1}, {"assignee_id": "nope"},
                                   {"assignee_id": 5}, {"description": "d" * 5001}, {"description": 5},
                                   {"unknown_field": 1}, {"id": str(uuid.uuid4())}])
def test_task_create_rejects_invalid_fields(client, make_user, extra):
    h = make_user()["headers"]
    pid = make_project(client, h)["id"]
    assert client.post(f"/projects/{pid}/tasks", json={"title": "ok", **extra}, headers=h).status_code == 422


@pytest.mark.functional
@pytest.mark.parametrize("body", [{}, {"title": None}, {"status": None}, {"description": None}, {"status": "x"},
                                  {"title": ""}, {"nope": 1}])
def test_task_patch_rejects_invalid_bodies(client, make_user, body):
    h = make_user()["headers"]
    pid = make_project(client, h)["id"]
    tid = make_task(client, h, pid)["id"]
    assert client.patch(f"/tasks/{tid}", json=body, headers=h).status_code == 422


@pytest.mark.functional
def test_malformed_path_ids_return_422(client, make_user):
    h = make_user()["headers"]
    assert client.get("/projects/not-a-uuid/tasks", headers=h).status_code == 422
    assert client.patch("/tasks/123", json={"status": "done"}, headers=h).status_code == 422
    assert client.delete("/projects/xyz", headers=h).status_code == 422


@pytest.mark.functional
@pytest.mark.hidden
@pytest.mark.parametrize("field", ["title", "description"])
def test_control_characters_are_rejected_not_500(client, make_user, field):
    h = make_user()["headers"]
    pid = make_project(client, h)["id"]
    for ch in ("\x00", "\x07", "\x1b"):
        body = {"title": "ok", field: f"a{ch}b"}
        assert client.post(f"/projects/{pid}/tasks", json=body, headers=h).status_code == 422
    assert client.post("/projects", json={"name": "a\x00b"}, headers=h).status_code == 422


@pytest.mark.functional
@pytest.mark.hidden
def test_login_validation(client):
    for body in ({}, {"email": "a@b.com"}, {"email": "a@b.com", "password": ""}, {"email": "\x00", "password": "x"}):
        assert client.post("/login", json=body).status_code in (401, 422)


# -------------------------------- database ---------------------------------
@pytest.mark.database
def test_rejected_input_creates_no_rows(client, make_user, db, params):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    users_before = db.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
    for body in ({"email": "bad", "password": _pw(params)}, {"email": unique_email(), "password": "x"},
                 {"email": unique_email(), "password": _pw(params), "role": "admin"}):
        client.post("/register", json=body)
    client.post("/projects", json={"name": " "}, headers=owner["headers"])
    client.post(f"/projects/{pid}/tasks", json={"title": ""}, headers=owner["headers"])
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == users_before
    assert db.execute("SELECT count(*) AS n FROM projects WHERE owner_id = %s", (owner["id"],)).fetchone()["n"] == 1
    assert db.execute("SELECT count(*) AS n FROM tasks WHERE project_id = %s", (pid,)).fetchone()["n"] == 0


@pytest.mark.database
@pytest.mark.parametrize("text", ["O'Brien \"quotes\"", "100% %s %(name)s {0} {x}", "C:\\path\\to \\' end", "emoji 🚀 日本語",
                                  "line1\nline2\ttab", "'; SELECT pg_sleep(0); --"])
def test_awkward_text_round_trips_exactly(client, make_user, db, text):
    h = make_user()["headers"]
    pid = make_project(client, h)["id"]
    t = make_task(client, h, pid, title="rt", description=text)
    assert t["description"] == text.strip()
    stored = db.execute("SELECT description FROM tasks WHERE id = %s", (t["id"],)).fetchone()["description"]
    assert stored == text.strip()


@pytest.mark.database
def test_user_with_apostrophe_email_registers_and_logs_in(client):
    email = f"o'brien{uuid.uuid4().hex[:6]}@example.com"
    r = client.post("/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201 and r.json()["email"] == email
    assert client.post("/login", json={"email": email, "password": PASSWORD}).status_code == 200


@pytest.mark.database
@pytest.mark.hidden
def test_injection_attempts_leave_users_table_intact(client, db):
    u = register_and_login(client)
    before = db.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
    for payload in SQLI_LOGIN:
        client.post("/login", json={"email": payload, "password": payload})
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == before
    assert db.execute("SELECT 1 AS ok FROM users WHERE id = %s", (u["id"],)).fetchone() is not None


@pytest.mark.database
def test_repository_returns_expected_rows(client, db):
    from app.repositories import users

    u = register_and_login(client)
    by_email, by_id = users.get_by_email(db, u["email"]), users.get_by_id(db, u["id"])
    assert by_email is not None and str(by_email["id"]) == u["id"] and by_id["email"] == u["email"]
    assert users.get_by_email(db, unique_email()) is None
    assert users.get_by_id(db, uuid.uuid4()) is None


# -------------------------------- security ---------------------------------
@pytest.mark.security
@pytest.mark.parametrize("payload", SQLI_LOGIN)
def test_sql_injection_in_login_never_authenticates(client, payload):
    register_and_login(client)
    for creds in ({"email": payload, "password": PASSWORD}, {"email": unique_email(), "password": payload}):
        r = client.post("/login", json=creds)
        assert r.status_code in (401, 422), (payload, r.status_code)


@pytest.mark.security
def test_mass_assignment_of_role_is_rejected(client, db, params):
    email = unique_email()
    r = client.post("/register", json={"email": email, "password": _pw(params), "role": "admin"})
    assert r.status_code == 422
    assert db.execute("SELECT count(*) AS n FROM users WHERE email = %s", (email,)).fetchone()["n"] == 0


@pytest.mark.security
@pytest.mark.parametrize("token", BAD_TOKENS)
def test_malformed_tokens_are_401_not_500(client, token):
    value = f"Bearer {token}".encode("latin-1")  # raw bytes: allows non-ASCII header values
    r = client.post("/projects", json={"name": "x"}, headers={"Authorization": value})
    assert_error_shape(r, 401)


@pytest.mark.security
def test_validation_errors_do_not_echo_secrets(client, params):
    secret = "Sup3r-Secret-" + uuid.uuid4().hex
    r = client.post("/register", json={"email": "bad", "password": secret[: params["password_min_length"] - 1]})
    assert r.status_code == 422
    assert secret[: params["password_min_length"] - 1] not in r.text and "bad" != ""
    r = client.post("/register", json={"email": unique_email(), "password": secret, "role": secret})
    assert secret not in r.text


@pytest.mark.security
@pytest.mark.hidden
def test_unhandled_exceptions_return_generic_500(migrated_url, monkeypatch):
    from app.main import create_app
    from app.repositories import users

    def boom(*a, **k):
        raise RuntimeError("db password=hunter2 host=10.0.0.5 SELECT * FROM users")

    monkeypatch.setattr(users, "get_by_email", boom)
    r = TestClient(create_app(), raise_server_exceptions=False).post(
        "/login", json={"email": unique_email(), "password": PASSWORD})
    assert_error_shape(r, 500)
    for leaked in ("hunter2", "10.0.0.5", "SELECT", "RuntimeError", "Traceback"):
        assert leaked not in r.text


@pytest.mark.security
@pytest.mark.hidden
def test_oversized_payloads_are_rejected_not_500(client, make_user):
    h = make_user()["headers"]
    huge = "A" * 2_000_000
    assert client.post("/projects", json={"name": huge}, headers=h).status_code in (413, 422)
    pid = make_project(client, h)["id"]
    assert client.post(f"/projects/{pid}/tasks", json={"title": "x", "description": huge}, headers=h).status_code in (413, 422)
    assert client.post("/login", json={"email": huge, "password": huge}).status_code in (401, 413, 422)


@pytest.mark.security
@pytest.mark.hidden
def test_malformed_json_and_wrong_content_type(client, make_user):
    h = {**make_user()["headers"], "Content-Type": "application/json"}
    assert client.post("/projects", content=b"{not json", headers=h).status_code == 422
    assert client.post("/projects", content=b"[]", headers=h).status_code == 422
    assert client.post("/projects", content=b"name=x", headers={**h, "Content-Type": "text/plain"}).status_code == 422


# --------------------------------- quality ---------------------------------
@pytest.mark.quality
def test_every_error_class_uses_one_shape(client, make_user):
    h = make_user()["headers"]
    cases = [client.get("/no-such-route"), client.put("/projects", json={}, headers=h),
             client.post("/projects", json={}, headers=h), client.post("/projects", json={"name": "x"}),
             client.delete(f"/projects/{uuid.uuid4()}", headers=h)]
    assert [c.status_code for c in cases] == [404, 405, 422, 401, 404]
    for c in cases:
        assert_error_shape(c)


@pytest.mark.quality
def test_validation_error_names_the_offending_field(client, make_user):
    err = assert_error_shape(client.post("/projects", json={"name": ""}, headers=make_user()["headers"]), 422)
    assert "name" in err["message"].lower()


@pytest.mark.quality
def test_errors_module_contract():
    from app import errors

    assert issubclass(errors.AppError, Exception)
    err = errors.AppError(418, "teapot", "short and stout")
    assert (err.status_code, err.code, err.message) == (418, "teapot", "short and stout")
    assert callable(errors.install_exception_handlers) and callable(errors.error_response)


@pytest.mark.quality
def test_schema_contract():
    from app import schemas

    for name in ("RegisterRequest", "LoginRequest", "UserOut", "TokenOut", "ProjectCreate", "ProjectOut",
                 "TaskCreate", "TaskUpdate", "TaskOut"):
        assert hasattr(schemas, name), name
    upd = schemas.TaskUpdate(assignee_id=None)
    assert upd.changes() == {"assignee_id": None}
    assert schemas.TaskUpdate(title="x").changes() == {"title": "x"}


@pytest.mark.quality
def test_users_repository_builds_no_sql_from_variables():
    """Heuristic static check: f-strings in SQL may only interpolate UPPER_CASE module constants."""
    from app.repositories import users

    tree = ast.parse(inspect.getsource(users))
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    assert isinstance(part.value, ast.Name) and part.value.id.lstrip("_").isupper(), ast.dump(part.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) \
                and isinstance(node.left.value, str) and "SELECT" in node.left.value.upper():
            pytest.fail("string-% interpolation used to build SQL")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" \
                and isinstance(node.func.value, ast.Constant) and "SELECT" in str(node.func.value.value).upper():
            pytest.fail(".format() used to build SQL")


@pytest.mark.quality
def test_users_repository_contract():
    from app.repositories import users

    assert list(inspect.signature(users.create_user).parameters) == ["conn", "email", "password_hash", "role"]
    assert list(inspect.signature(users.get_by_email).parameters) == ["conn", "email"]
    assert list(inspect.signature(users.get_by_id).parameters) == ["conn", "user_id"]

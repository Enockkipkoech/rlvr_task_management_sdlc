"""Task 4 grader: authorization matrix and business rules.

Owned file: app/services/project_service.py
"""

from __future__ import annotations

import inspect
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import psycopg
import pytest

from helpers import assert_error_shape, make_project, make_task

ACTORS = ["owner", "admin", "assignee", "stranger", "unauthenticated"]
OPS = ["create_task", "list_tasks", "patch_status_own", "patch_status_other", "patch_title",
       "reassign", "delete_project"]
EXPECTED = {
    "owner":           [201, 200, 200, 200, 200, 200, 204],
    "admin":           [201, 200, 200, 200, 200, 200, 204],
    "assignee":        [403, 200, 200, 403, 403, 403, 403],
    "stranger":        [403, 403, 403, 403, 403, 403, 403],
    "unauthenticated": [401, 401, 401, 401, 401, 401, 401],
}


def build_world(client, make_user):
    owner, admin = make_user(), make_user(role="admin")
    assignee, stranger = make_user(), make_user()
    project = make_project(client, owner["headers"], "ConfidentialAlpha")
    t_assigned = make_task(client, owner["headers"], project["id"], "assigned-secret", assignee_id=assignee["id"])
    t_other = make_task(client, owner["headers"], project["id"], "unassigned-secret")
    return {"owner": owner, "admin": admin, "assignee": assignee, "stranger": stranger,
            "unauthenticated": {"id": None, "headers": {}}, "project": project,
            "t_assigned": t_assigned, "t_other": t_other}


def run_op(client, w, actor, op):
    h = w[actor]["headers"]
    pid = w["project"]["id"]
    return {
        "create_task": lambda: client.post(f"/projects/{pid}/tasks", json={"title": "new"}, headers=h),
        "list_tasks": lambda: client.get(f"/projects/{pid}/tasks", headers=h),
        "patch_status_own": lambda: client.patch(f"/tasks/{w['t_assigned']['id']}", json={"status": "in_progress"}, headers=h),
        "patch_status_other": lambda: client.patch(f"/tasks/{w['t_other']['id']}", json={"status": "done"}, headers=h),
        "patch_title": lambda: client.patch(f"/tasks/{w['t_assigned']['id']}", json={"title": "renamed"}, headers=h),
        "reassign": lambda: client.patch(f"/tasks/{w['t_assigned']['id']}",
                                         json={"assignee_id": w[actor]["id"] or str(uuid.uuid4())}, headers=h),
        "delete_project": lambda: client.delete(f"/projects/{pid}", headers=h),
    }[op]()


# ------------------------------- functional --------------------------------
@pytest.mark.functional
@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("op", OPS)
def test_role_matrix(client, make_user, actor, op):
    w = build_world(client, make_user)
    assert run_op(client, w, actor, op).status_code == EXPECTED[actor][OPS.index(op)]


@pytest.mark.functional
def test_in_progress_task_blocks_deletion_with_409(client, make_user):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    make_task(client, owner["headers"], pid, "a", status="todo")
    busy = make_task(client, owner["headers"], pid, "b", status="in_progress")
    make_task(client, owner["headers"], pid, "c", status="done")
    assert_error_shape(client.delete(f"/projects/{pid}", headers=owner["headers"]), 409)
    client.patch(f"/tasks/{busy['id']}", json={"status": "done"}, headers=owner["headers"])
    assert client.delete(f"/projects/{pid}", headers=owner["headers"]).status_code == 204


@pytest.mark.functional
def test_todo_and_done_tasks_do_not_block_deletion(client, make_user):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    make_task(client, owner["headers"], pid, "a", status="todo")
    make_task(client, owner["headers"], pid, "b", status="done")
    assert client.delete(f"/projects/{pid}", headers=owner["headers"]).status_code == 204


@pytest.mark.functional
def test_assignee_status_change_is_blocking_afterwards(client, make_user):
    w = build_world(client, make_user)
    assert run_op(client, w, "assignee", "patch_status_own").status_code == 200
    assert run_op(client, w, "owner", "delete_project").status_code == 409


@pytest.mark.functional
@pytest.mark.hidden
def test_assignee_sees_only_their_own_tasks(client, make_user):
    w = build_world(client, make_user)
    r = client.get(f"/projects/{w['project']['id']}/tasks", headers=w["assignee"]["headers"])
    assert [t["id"] for t in r.json()] == [w["t_assigned"]["id"]]


@pytest.mark.functional
@pytest.mark.hidden
def test_access_is_scoped_per_project(client, make_user):
    a, b = make_user(), make_user()
    pa = make_project(client, a["headers"], "A")["id"]
    pb = make_project(client, b["headers"], "B")["id"]
    assert client.get(f"/projects/{pb}/tasks", headers=a["headers"]).status_code == 403
    assert client.get(f"/projects/{pa}/tasks", headers=b["headers"]).status_code == 403
    assert client.get(f"/projects/{pa}/tasks", headers=a["headers"]).status_code == 200


@pytest.mark.functional
@pytest.mark.hidden
def test_unassigned_user_loses_access(client, make_user):
    w = build_world(client, make_user)
    client.patch(f"/tasks/{w['t_assigned']['id']}", json={"assignee_id": None}, headers=w["owner"]["headers"])
    assert run_op(client, w, "assignee", "list_tasks").status_code == 403
    assert run_op(client, w, "assignee", "patch_status_own").status_code == 403


@pytest.mark.functional
@pytest.mark.hidden
def test_owner_can_reassign_to_another_user(client, make_user):
    w = build_world(client, make_user)
    other = make_user()
    r = client.patch(f"/tasks/{w['t_other']['id']}", json={"assignee_id": other["id"]}, headers=w["owner"]["headers"])
    assert r.status_code == 200 and r.json()["assignee_id"] == other["id"]
    assert client.patch(f"/tasks/{w['t_other']['id']}", json={"status": "done"}, headers=other["headers"]).status_code == 200


# -------------------------------- database ---------------------------------
def _counts(db, pid):
    return (db.execute("SELECT count(*) AS n FROM projects WHERE id = %s", (pid,)).fetchone()["n"],
            db.execute("SELECT count(*) AS n FROM tasks WHERE project_id = %s", (pid,)).fetchone()["n"])


@pytest.mark.database
def test_blocked_delete_changes_nothing(client, make_user, db):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    for i, s in enumerate(["todo", "in_progress", "done"]):
        make_task(client, owner["headers"], pid, f"t{i}", status=s)
    assert client.delete(f"/projects/{pid}", headers=owner["headers"]).status_code == 409
    assert _counts(db, pid) == (1, 3)


@pytest.mark.database
def test_delete_cascades_and_spares_other_projects(client, make_user, db):
    owner = make_user()
    drop = make_project(client, owner["headers"], "drop")["id"]
    keep = make_project(client, owner["headers"], "keep")["id"]
    make_task(client, owner["headers"], drop, "x", status="done")
    make_task(client, owner["headers"], keep, "y", status="in_progress")
    assert client.delete(f"/projects/{drop}", headers=owner["headers"]).status_code == 204
    assert _counts(db, drop) == (0, 0) and _counts(db, keep) == (1, 1)


@pytest.mark.database
def test_assignee_removal_sets_null_but_keeps_task(client, make_user, db):
    w = build_world(client, make_user)
    db.execute("DELETE FROM users WHERE id = %s", (w["assignee"]["id"],))
    rows = client.get(f"/projects/{w['project']['id']}/tasks", headers=w["owner"]["headers"]).json()
    assert len(rows) == 2 and all(t["assignee_id"] is None for t in rows if t["id"] == w["t_assigned"]["id"])


def _race(client_factory, make_user, monkeypatch, competing: str):
    """Fire a competing write *between* the in-progress check and the DELETE.

    The hook runs just before the project DELETE statement executes.  A correct
    implementation holds a lock that makes the competing request wait (so it
    cannot finish inside the join window) and it later fails with 404/409.
    A racy implementation lets the competing write commit first, after which
    the DELETE still succeeds -- an in_progress task was silently destroyed.
    """
    c = client_factory()
    owner = make_user()
    pid = make_project(c, owner["headers"], "race")["id"]
    tid = make_task(c, owner["headers"], pid, "victim")["id"]
    result: dict = {}

    def compete():
        cc = client_factory()
        if competing == "patch":
            result["status"] = cc.patch(f"/tasks/{tid}", json={"status": "in_progress"},
                                        headers=owner["headers"]).status_code
        else:
            result["status"] = cc.post(f"/projects/{pid}/tasks", json={"title": "late", "status": "in_progress"},
                                       headers=owner["headers"]).status_code

    thread = threading.Thread(target=compete)
    fired = {"n": 0}
    original = psycopg.Cursor.execute

    def hooked(self, query, *args, **kwargs):
        try:
            text = query if isinstance(query, str) else query.as_string(self.connection)
        except Exception:
            text = ""
        if fired["n"] == 0 and re.match(r"\s*DELETE\s+FROM\s+(public\.)?projects\b", text, flags=re.I):
            fired["n"] = 1
            thread.start()
            thread.join(timeout=1.0)
        return original(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", hooked)
    deleted = c.delete(f"/projects/{pid}", headers=owner["headers"]).status_code
    monkeypatch.setattr(psycopg.Cursor, "execute", original)
    if thread.is_alive() or fired["n"]:
        thread.join(timeout=15)
    return fired["n"], result.get("status"), deleted


@pytest.mark.database
@pytest.mark.hidden
@pytest.mark.parametrize("competing", ["patch", "create"])
def test_delete_cannot_race_with_in_progress_writes(migrated_url, make_user, monkeypatch, competing):
    from fastapi.testclient import TestClient

    from app.main import create_app

    fired, other, deleted = _race(lambda: TestClient(create_app()), make_user, monkeypatch, competing)
    assert fired == 1, "the project DELETE statement was never issued through psycopg"
    assert deleted in (204, 409)
    assert not (other in (200, 201) and deleted == 204), (
        f"competing {competing} succeeded ({other}) and the project was deleted ({deleted})")


@pytest.mark.database
@pytest.mark.hidden
def test_delete_and_transition_serialise_under_threads(migrated_url, make_user):
    from fastapi.testclient import TestClient

    from app.main import create_app

    owner = make_user()
    bad = 0
    for _ in range(12):
        c = TestClient(create_app())
        pid = make_project(c, owner["headers"], "t")["id"]
        tid = make_task(c, owner["headers"], pid, "x")["id"]
        out = {}
        a = threading.Thread(target=lambda: out.update(d=TestClient(create_app()).delete(f"/projects/{pid}", headers=owner["headers"]).status_code))
        b = threading.Thread(target=lambda: out.update(p=TestClient(create_app()).patch(f"/tasks/{tid}", json={"status": "in_progress"}, headers=owner["headers"]).status_code))
        a.start(); b.start(); a.join(); b.join()
        bad += out["d"] == 204 and out["p"] == 200
    assert bad == 0


# -------------------------------- security ---------------------------------
@pytest.mark.security
@pytest.mark.parametrize("op", OPS)
def test_stranger_responses_leak_nothing(client, make_user, op):
    w = build_world(client, make_user)
    text = run_op(client, w, "stranger", op).text
    for secret in ("ConfidentialAlpha", "assigned-secret", "unassigned-secret", w["owner"]["id"],
                   w["owner"]["email"], w["assignee"]["id"]):
        assert secret not in text


@pytest.mark.security
@pytest.mark.hidden
def test_role_claim_inside_token_is_not_trusted(client, make_user, jwt_secret):
    w = build_world(client, make_user)
    now = datetime.now(timezone.utc)
    forged = jwt.encode({"sub": w["stranger"]["id"], "role": "admin", "iat": now,
                         "exp": now + timedelta(minutes=5)}, jwt_secret, algorithm="HS256")
    r = client.delete(f"/projects/{w['project']['id']}", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 403


@pytest.mark.security
@pytest.mark.hidden
def test_assignee_cannot_touch_a_task_assigned_to_someone_else(client, make_user):
    w = build_world(client, make_user)
    second = make_user()
    t = make_task(client, w["owner"]["headers"], w["project"]["id"], "second", assignee_id=second["id"])
    r = client.patch(f"/tasks/{t['id']}", json={"status": "done"}, headers=w["assignee"]["headers"])
    assert r.status_code == 403


@pytest.mark.security
@pytest.mark.hidden
@pytest.mark.parametrize("body", [{"project_id": str(uuid.uuid4())}, {"id": str(uuid.uuid4())},
                                  {"status": "done", "role": "admin"}])
def test_assignee_cannot_smuggle_fields(client, make_user, body):
    w = build_world(client, make_user)
    r = client.patch(f"/tasks/{w['t_assigned']['id']}", json=body, headers=w["assignee"]["headers"])
    assert r.status_code in (403, 422)
    after = client.get(f"/projects/{w['project']['id']}/tasks", headers=w["owner"]["headers"]).json()
    assert {t["id"] for t in after} == {w["t_assigned"]["id"], w["t_other"]["id"]}


@pytest.mark.security
def test_denials_never_expose_internals(client, make_user):
    w = build_world(client, make_user)
    for actor, op in [("stranger", "delete_project"), ("assignee", "patch_title"), ("owner", "delete_project")]:
        text = run_op(client, w, actor, op).text.lower()
        for needle in ("traceback", "psycopg", "select ", "insert ", "postgres"):
            assert needle not in text


# --------------------------------- quality ---------------------------------
@pytest.mark.quality
def test_denial_and_conflict_use_error_shape(client, make_user):
    w = build_world(client, make_user)
    assert_error_shape(run_op(client, w, "stranger", "list_tasks"), 403)
    client.patch(f"/tasks/{w['t_other']['id']}", json={"status": "in_progress"}, headers=w["owner"]["headers"])
    assert_error_shape(run_op(client, w, "owner", "delete_project"), 409)


@pytest.mark.quality
def test_service_contract():
    from app.services import project_service as svc

    expected = {"create_project": ["conn", "user", "name"], "create_task": ["conn", "user", "project_id", "data"],
                "list_tasks": ["conn", "user", "project_id"], "update_task": ["conn", "user", "task_id", "changes"],
                "delete_project": ["conn", "user", "project_id"]}
    for name, params in expected.items():
        assert list(inspect.signature(getattr(svc, name)).parameters) == params, name


@pytest.mark.quality
def test_service_is_framework_agnostic():
    from app.services import project_service as svc

    src = inspect.getsource(svc)
    assert "HTTPException" not in src and "from fastapi" not in src and "import fastapi" not in src


@pytest.mark.quality
def test_delete_runs_inside_an_explicit_transaction():
    from app.services import project_service as svc

    src = inspect.getsource(svc.delete_project)
    assert "transaction" in src or "BEGIN" in src.upper()


@pytest.mark.quality
def test_not_found_is_distinct_from_forbidden(client, make_user):
    w = build_world(client, make_user)
    ghost = str(uuid.uuid4())
    assert client.get(f"/projects/{ghost}/tasks", headers=w["stranger"]["headers"]).status_code == 404
    assert client.get(f"/projects/{w['project']['id']}/tasks", headers=w["stranger"]["headers"]).status_code == 403

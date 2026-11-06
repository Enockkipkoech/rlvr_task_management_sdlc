"""Task 3 grader: project and task CRUD.

Owned files: app/repositories/projects.py, app/repositories/tasks.py,
app/routers/projects.py, app/routers/tasks.py
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime

import pytest

from helpers import assert_error_shape, make_project, make_task

TASK_KEYS = {"id", "project_id", "assignee_id", "title", "description", "status",
             "created_at", "updated_at"}
INJECTIONS = ["'; DROP TABLE tasks; --", "Robert'); DROP TABLE users;--", "' OR '1'='1",
              '" OR 1=1 --', "100% done %s %(x)s", "back\\slash \\' end", "emoji 🚀 ünïcödé"]


# ------------------------------- functional --------------------------------
@pytest.mark.functional
def test_create_project(client, make_user):
    owner = make_user()
    r = client.post("/projects", json={"name": "Apollo"}, headers=owner["headers"])
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Apollo" and body["owner_id"] == owner["id"]
    uuid.UUID(body["id"])


@pytest.mark.functional
def test_create_task_defaults(client, make_user):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    task = make_task(client, owner["headers"], pid, "Write spec")
    assert task["title"] == "Write spec" and task["status"] == "todo"
    assert task["description"] == "" and task["assignee_id"] is None
    assert task["project_id"] == pid


@pytest.mark.functional
def test_create_task_with_all_fields(client, make_user):
    owner, helper = make_user(), make_user()
    pid = make_project(client, owner["headers"])["id"]
    task = make_task(client, owner["headers"], pid, "T", description="details",
                     status="in_progress", assignee_id=helper["id"])
    assert (task["description"], task["status"], task["assignee_id"]) == ("details", "in_progress", helper["id"])


@pytest.mark.functional
def test_list_tasks_in_creation_order(client, make_user):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    for i in range(4):
        make_task(client, owner["headers"], pid, f"task-{i}")
    r = client.get(f"/projects/{pid}/tasks", headers=owner["headers"])
    assert r.status_code == 200
    assert [t["title"] for t in r.json()] == [f"task-{i}" for i in range(4)]


@pytest.mark.functional
def test_list_empty_project_returns_empty_list(client, make_user):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    r = client.get(f"/projects/{pid}/tasks", headers=owner["headers"])
    assert r.status_code == 200 and r.json() == []


@pytest.mark.functional
def test_list_only_returns_tasks_of_that_project(client, make_user):
    owner = make_user()
    a, b = (make_project(client, owner["headers"], n)["id"] for n in "AB")
    make_task(client, owner["headers"], a, "in-a")
    make_task(client, owner["headers"], b, "in-b")
    assert [t["title"] for t in client.get(f"/projects/{a}/tasks", headers=owner["headers"]).json()] == ["in-a"]


@pytest.mark.functional
def test_patch_task_fields_and_persist(client, make_user):
    owner, helper = make_user(), make_user()
    pid = make_project(client, owner["headers"])["id"]
    tid = make_task(client, owner["headers"], pid, "old")["id"]
    r = client.patch(f"/tasks/{tid}", headers=owner["headers"],
                     json={"title": "new", "description": "d", "status": "done", "assignee_id": helper["id"]})
    assert r.status_code == 200
    assert (r.json()["title"], r.json()["status"], r.json()["assignee_id"]) == ("new", "done", helper["id"])
    listed = client.get(f"/projects/{pid}/tasks", headers=owner["headers"]).json()[0]
    assert listed["title"] == "new" and listed["description"] == "d"


@pytest.mark.functional
def test_patch_is_partial_and_can_unassign(client, make_user):
    owner, helper = make_user(), make_user()
    pid = make_project(client, owner["headers"])["id"]
    tid = make_task(client, owner["headers"], pid, "keep", assignee_id=helper["id"])["id"]
    r = client.patch(f"/tasks/{tid}", json={"status": "in_progress"}, headers=owner["headers"])
    assert r.json()["title"] == "keep" and r.json()["assignee_id"] == helper["id"]
    r = client.patch(f"/tasks/{tid}", json={"assignee_id": None}, headers=owner["headers"])
    assert r.status_code == 200 and r.json()["assignee_id"] is None


@pytest.mark.functional
def test_delete_project_returns_204_and_removes_it(client, make_user):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    make_task(client, owner["headers"], pid)
    r = client.delete(f"/projects/{pid}", headers=owner["headers"])
    assert r.status_code == 204 and r.content == b""
    assert client.get(f"/projects/{pid}/tasks", headers=owner["headers"]).status_code == 404


@pytest.mark.functional
@pytest.mark.parametrize("call", ["create_task", "list", "delete", "patch"])
def test_unknown_ids_return_404(client, make_user, call):
    owner = make_user()
    ghost = str(uuid.uuid4())
    r = {
        "create_task": lambda: client.post(f"/projects/{ghost}/tasks", json={"title": "t"}, headers=owner["headers"]),
        "list": lambda: client.get(f"/projects/{ghost}/tasks", headers=owner["headers"]),
        "delete": lambda: client.delete(f"/projects/{ghost}", headers=owner["headers"]),
        "patch": lambda: client.patch(f"/tasks/{ghost}", json={"status": "done"}, headers=owner["headers"]),
    }[call]()
    assert_error_shape(r, 404)


# -------------------------------- database ---------------------------------
@pytest.mark.database
def test_rows_are_persisted_exactly(client, make_user, db):
    owner = make_user()
    p = make_project(client, owner["headers"], "Persisted")
    t = make_task(client, owner["headers"], p["id"], "Row", description="body", status="done")
    prow = db.execute("SELECT name, owner_id FROM projects WHERE id = %s", (p["id"],)).fetchone()
    trow = db.execute("SELECT title, description, status, project_id FROM tasks WHERE id = %s", (t["id"],)).fetchone()
    assert (prow["name"], str(prow["owner_id"])) == ("Persisted", owner["id"])
    assert (trow["title"], trow["description"], trow["status"], str(trow["project_id"])) == ("Row", "body", "done", p["id"])


@pytest.mark.database
def test_delete_cascades_tasks_in_database(client, make_user, db):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    ids = [make_task(client, owner["headers"], pid, f"t{i}", status=s)["id"] for i, s in enumerate(["todo", "done"])]
    assert client.delete(f"/projects/{pid}", headers=owner["headers"]).status_code == 204
    assert db.execute("SELECT count(*) AS n FROM tasks WHERE id = ANY(%s::uuid[])", (ids,)).fetchone()["n"] == 0
    assert db.execute("SELECT count(*) AS n FROM projects WHERE id = %s", (pid,)).fetchone()["n"] == 0


@pytest.mark.database
def test_removed_assignee_shows_as_null(client, make_user, db):
    owner, helper = make_user(), make_user()
    pid = make_project(client, owner["headers"])["id"]
    make_task(client, owner["headers"], pid, assignee_id=helper["id"])
    db.execute("DELETE FROM users WHERE id = %s", (helper["id"],))
    assert client.get(f"/projects/{pid}/tasks", headers=owner["headers"]).json()[0]["assignee_id"] is None


@pytest.mark.database
@pytest.mark.parametrize("op", ["create", "patch"])
def test_unknown_assignee_returns_422_and_changes_nothing(client, make_user, db, op):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    ghost = str(uuid.uuid4())
    if op == "create":
        r = client.post(f"/projects/{pid}/tasks", json={"title": "x", "assignee_id": ghost}, headers=owner["headers"])
        assert r.status_code == 422
        assert db.execute("SELECT count(*) AS n FROM tasks WHERE project_id = %s", (pid,)).fetchone()["n"] == 0
    else:
        tid = make_task(client, owner["headers"], pid, "x")["id"]
        r = client.patch(f"/tasks/{tid}", json={"assignee_id": ghost}, headers=owner["headers"])
        assert r.status_code == 422
        assert db.execute("SELECT assignee_id FROM tasks WHERE id = %s", (tid,)).fetchone()["assignee_id"] is None


@pytest.mark.database
@pytest.mark.hidden
def test_patch_advances_updated_at_only(client, make_user, db):
    owner = make_user()
    pid = make_project(client, owner["headers"])["id"]
    tid = make_task(client, owner["headers"], pid, "x")["id"]
    before = db.execute("SELECT created_at, updated_at FROM tasks WHERE id = %s", (tid,)).fetchone()
    client.patch(f"/tasks/{tid}", json={"title": "y"}, headers=owner["headers"])
    after = db.execute("SELECT created_at, updated_at FROM tasks WHERE id = %s", (tid,)).fetchone()
    assert after["created_at"] == before["created_at"] and after["updated_at"] > before["updated_at"]


@pytest.mark.database
@pytest.mark.hidden
def test_delete_leaves_other_projects_untouched(client, make_user):
    owner = make_user()
    keep, drop = (make_project(client, owner["headers"], n)["id"] for n in ("keep", "drop"))
    make_task(client, owner["headers"], keep, "survivor")
    make_task(client, owner["headers"], drop, "victim")
    client.delete(f"/projects/{drop}", headers=owner["headers"])
    assert [t["title"] for t in client.get(f"/projects/{keep}/tasks", headers=owner["headers"]).json()] == ["survivor"]


# -------------------------------- security ---------------------------------
@pytest.mark.security
@pytest.mark.parametrize("payload", INJECTIONS)
def test_injection_payloads_are_stored_verbatim(client, make_user, db, payload):
    owner = make_user()
    p = client.post("/projects", json={"name": payload[:100]}, headers=owner["headers"])
    assert p.status_code == 201 and p.json()["name"] == payload[:100].strip()
    pid = p.json()["id"]
    t = client.post(f"/projects/{pid}/tasks", json={"title": payload[:100], "description": payload},
                    headers=owner["headers"])
    assert t.status_code == 201
    assert t.json()["description"] == payload.strip()
    patched = client.patch(f"/tasks/{t.json()['id']}", json={"title": payload[:100]}, headers=owner["headers"])
    assert patched.status_code == 200 and patched.json()["title"] == payload[:100].strip()
    for table in ("users", "projects", "tasks"):
        assert db.execute("SELECT to_regclass(%s) AS r", (f"public.{table}",)).fetchone()["r"] is not None


@pytest.mark.security
def test_owner_id_in_body_cannot_be_forged(client, make_user):
    owner, victim = make_user(), make_user()
    r = client.post("/projects", json={"name": "x", "owner_id": victim["id"]}, headers=owner["headers"])
    assert r.status_code == 422 or r.json().get("owner_id") == owner["id"]


@pytest.mark.security
@pytest.mark.hidden
def test_project_id_cannot_be_moved_via_patch(client, make_user):
    owner = make_user()
    a, b = (make_project(client, owner["headers"], n)["id"] for n in "AB")
    tid = make_task(client, owner["headers"], a)["id"]
    r = client.patch(f"/tasks/{tid}", json={"project_id": b}, headers=owner["headers"])
    assert r.status_code == 422
    assert client.get(f"/projects/{a}/tasks", headers=owner["headers"]).json()[0]["id"] == tid


@pytest.mark.security
@pytest.mark.parametrize("method,path", [("post", "/projects"), ("get", "/projects/{id}/tasks"),
                                         ("post", "/projects/{id}/tasks"), ("patch", "/tasks/{id}"),
                                         ("delete", "/projects/{id}")])
def test_every_route_requires_authentication(client, method, path):
    body = {"name": "x"} if path == "/projects" else {"title": "x"}
    r = client.request(method.upper(), path.format(id=uuid.uuid4()), json=body if method != "get" else None)
    assert r.status_code == 401


# --------------------------------- quality ---------------------------------
@pytest.mark.quality
def test_response_shapes_are_exact(client, make_user):
    owner = make_user()
    p = make_project(client, owner["headers"])
    assert set(p) == {"id", "name", "owner_id"}
    t = make_task(client, owner["headers"], p["id"])
    assert set(t) == TASK_KEYS
    assert all(set(x) == TASK_KEYS for x in client.get(f"/projects/{p['id']}/tasks", headers=owner["headers"]).json())
    datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))


@pytest.mark.quality
def test_openapi_declares_crud_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/projects"]
    assert {"post", "get"} <= set(paths["/projects/{project_id}/tasks"])
    assert "delete" in paths["/projects/{project_id}"]
    assert "patch" in paths["/tasks/{task_id}"]


@pytest.mark.quality
def test_success_status_codes(client, make_user):
    owner = make_user()
    h = owner["headers"]
    p = client.post("/projects", json={"name": "c"}, headers=h)
    t = client.post(f"/projects/{p.json()['id']}/tasks", json={"title": "c"}, headers=h)
    codes = [p.status_code, t.status_code,
             client.get(f"/projects/{p.json()['id']}/tasks", headers=h).status_code,
             client.patch(f"/tasks/{t.json()['id']}", json={"status": "done"}, headers=h).status_code,
             client.delete(f"/projects/{p.json()['id']}", headers=h).status_code]
    assert codes == [201, 201, 200, 200, 204]


@pytest.mark.quality
def test_repository_contract():
    from app.repositories import projects, tasks

    expected = {
        projects: {"create": ["conn", "name", "owner_id"], "get": ["conn", "project_id"],
                   "lock": ["conn", "project_id", "exclusive"],
                   "has_in_progress_tasks": ["conn", "project_id"], "delete": ["conn", "project_id"]},
        tasks: {"create": ["conn", "project_id", "title", "description", "status", "assignee_id"],
                "get": ["conn", "task_id"], "list_for_project": ["conn", "project_id", "assignee_id"],
                "update": ["conn", "task_id", "changes"],
                "user_has_task_in_project": ["conn", "project_id", "user_id"]},
    }
    for module, funcs in expected.items():
        for name, params in funcs.items():
            got = list(inspect.signature(getattr(module, name)).parameters)
            assert got == params, f"{module.__name__}.{name}: {got}"


@pytest.mark.quality
def test_routers_expose_router_objects():
    from app.routers import projects, tasks

    assert hasattr(projects, "router") and hasattr(tasks, "router")

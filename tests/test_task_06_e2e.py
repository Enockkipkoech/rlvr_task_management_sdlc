"""Task 6 grader: end-to-end integration and quality.

Owned files: app/main.py, tests/test_e2e.py, Dockerfile, docker-compose.yml,
.env.example, README.md
"""

from __future__ import annotations

import ast
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import psycopg
import pytest
import yaml

from conftest import CANDIDATE_DIR, reset_schema
from helpers import PASSWORD, assert_error_shape, make_project, make_task, register_and_login, unique_email

COVERAGE_THRESHOLD = 80.0
MIN_CANDIDATE_TESTS = 10
ENDPOINTS = [("post", "/register"), ("post", "/login"), ("post", "/projects"),
             ("post", "/projects/{project_id}/tasks"), ("get", "/projects/{project_id}/tasks"),
             ("patch", "/tasks/{task_id}"), ("delete", "/projects/{project_id}")]
README_PATHS = ["/register", "/login", "/projects", "/projects/{id}/tasks", "/tasks/{id}"]
ENV_KEYS = ["DATABASE_URL", "JWT_SECRET", "ACCESS_TOKEN_TTL_MINUTES", "PASSWORD_MIN_LENGTH", "TITLE_MAX_LENGTH"]


def _clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMR_") and k != "PYTEST_ADDOPTS"}
    env.update(extra)
    return env


def _read(name: str) -> str:
    path = CANDIDATE_DIR / name
    assert path.is_file(), f"{name} is missing"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def candidate_run(database_url, tmp_path_factory):
    """Run the candidate's own tests (with coverage) against a clean, migrated database."""
    from app.migrate import run_migrations

    reset_schema(database_url)
    run_migrations(database_url)
    cov = tmp_path_factory.mktemp("cov") / "coverage.json"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "--tb=short",
         f"--cov=app", f"--cov-report=json:{cov}"],
        cwd=CANDIDATE_DIR, env=_clean_env(DATABASE_URL=database_url), capture_output=True,
        text=True, timeout=240,
    )
    percent = None
    if cov.is_file():
        percent = json.loads(cov.read_text())["totals"]["percent_covered"]
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", proc.stdout)) else 0
    reset_schema(database_url)
    run_migrations(database_url)
    return {"returncode": proc.returncode, "passed": passed, "coverage": percent, "output": proc.stdout[-1500:]}


# ------------------------------- functional --------------------------------
@pytest.mark.functional
def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.functional
def test_openapi_lists_every_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]
    for method, path in ENDPOINTS:
        assert method in paths.get(path, {}), f"{method.upper()} {path} missing"


@pytest.mark.functional
def test_full_flow_through_http(client):
    owner = register_and_login(client)
    helper = register_and_login(client)
    pid = make_project(client, owner["headers"], "E2E")["id"]
    t1 = make_task(client, owner["headers"], pid, "one", assignee_id=helper["id"])
    make_task(client, owner["headers"], pid, "two")
    assert client.patch(f"/tasks/{t1['id']}", json={"status": "in_progress"}, headers=helper["headers"]).status_code == 200
    assert client.delete(f"/projects/{pid}", headers=owner["headers"]).status_code == 409
    assert client.patch(f"/tasks/{t1['id']}", json={"status": "done"}, headers=owner["headers"]).status_code == 200
    assert client.delete(f"/projects/{pid}", headers=owner["headers"]).status_code == 204
    assert client.get(f"/projects/{pid}/tasks", headers=owner["headers"]).status_code == 404


@pytest.mark.functional
def test_candidate_test_suite_passes(candidate_run):
    assert candidate_run["returncode"] == 0, candidate_run["output"]
    assert candidate_run["passed"] >= MIN_CANDIDATE_TESTS, candidate_run["output"]


@pytest.mark.functional
def test_create_app_factory_returns_independent_apps(migrated_url):
    from fastapi import FastAPI

    from app.main import app, create_app

    a, b = create_app(), create_app()
    assert isinstance(a, FastAPI) and isinstance(app, FastAPI) and a is not b


@pytest.mark.functional
def test_server_starts_with_uvicorn(migrated_url, jwt_secret):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=CANDIDATE_DIR, env=_clean_env(DATABASE_URL=migrated_url, JWT_SECRET=jwt_secret),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        body = None
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                    body = json.loads(resp.read())
                break
            except Exception:
                time.sleep(0.25)
        assert body == {"status": "ok"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# -------------------------------- database ---------------------------------
@pytest.mark.database
def test_clean_database_setup_via_migration_command(database_url, jwt_secret):
    reset_schema(database_url)
    env = _clean_env(DATABASE_URL=database_url, JWT_SECRET=jwt_secret)
    for _ in range(2):  # second run must be a no-op
        proc = subprocess.run([sys.executable, "-m", "app.migrate"], cwd=CANDIDATE_DIR, env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr[-800:]
    with psycopg.connect(database_url, autocommit=True) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        applied = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert {"users", "projects", "tasks", "schema_migrations"} <= tables
    assert applied == sorted(p.name for p in (CANDIDATE_DIR / "migrations").glob("*.sql"))


@pytest.mark.database
def test_health_is_503_when_database_is_unreachable(client, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=1")
    assert_error_shape(client.get("/health"), 503)


@pytest.mark.database
@pytest.mark.hidden
def test_flow_leaves_no_orphans(client, db):
    owner = register_and_login(client)
    pid = make_project(client, owner["headers"], "orphans")["id"]
    for i in range(3):
        make_task(client, owner["headers"], pid, f"t{i}", status="done")
    assert client.delete(f"/projects/{pid}", headers=owner["headers"]).status_code == 204
    orphans = db.execute("SELECT count(*) AS n FROM tasks t LEFT JOIN projects p ON p.id = t.project_id "
                         "WHERE p.id IS NULL").fetchone()["n"]
    assert orphans == 0


# -------------------------------- security ---------------------------------
def _dockerfile_lines() -> list[str]:
    return [ln.strip() for ln in _read("Dockerfile").splitlines() if ln.strip() and not ln.strip().startswith("#")]


@pytest.mark.security
def test_container_runs_as_non_root():
    users = [ln.split(None, 1)[1].strip() for ln in _dockerfile_lines() if ln.upper().startswith("USER ")]
    assert users and users[-1].split(":")[0] not in ("root", "0")


@pytest.mark.security
def test_dockerfile_bakes_in_no_secrets():
    text = _read("Dockerfile")
    assert not re.search(r"^\s*(ENV|ARG)\s+[^\n]*(JWT_SECRET|PASSWORD|SECRET)[ =]\S+", text, flags=re.M | re.I)
    assert not re.search(r"COPY\s+[^\n]*\.env(\s|$)", text)


@pytest.mark.security
def test_base_image_is_pinned():
    first = next(ln for ln in _dockerfile_lines() if ln.upper().startswith("FROM "))
    image = first.split()[1]
    assert ":" in image and not image.endswith(":latest")


@pytest.mark.security
def test_compose_does_not_hardcode_secrets():
    data = yaml.safe_load(_read("docker-compose.yml"))
    api_env = data["services"]["api"]["environment"]
    api_env = api_env if isinstance(api_env, dict) else dict(x.split("=", 1) for x in api_env)
    assert str(api_env["JWT_SECRET"]).startswith("${")
    db_env = data["services"]["db"]["environment"]
    db_env = db_env if isinstance(db_env, dict) else dict(x.split("=", 1) for x in db_env)
    assert str(db_env["POSTGRES_PASSWORD"]).startswith("${")


@pytest.mark.security
def test_env_example_has_no_real_secret():
    values = dict(ln.split("=", 1) for ln in _read(".env.example").splitlines() if "=" in ln and not ln.startswith("#"))
    secret = values["JWT_SECRET"].strip()
    assert secret == "" or re.search(r"change|replace|example|placeholder|your", secret, flags=re.I)
    assert not (len(secret) >= 32 and re.fullmatch(r"[A-Za-z0-9_\-+/=]+", secret) and not re.search(r"change|replace|example", secret, re.I))


REGRESSIONS = ["alg_none", "stranger_delete", "sqli_login", "role_mass_assignment", "expired_token",
               "unauth_create_project", "unauth_list", "unauth_patch", "unauth_delete", "idor_patch"]


@pytest.mark.security
@pytest.mark.hidden
@pytest.mark.parametrize("case", REGRESSIONS)
def test_security_regressions(client, make_user, jwt_secret, case):
    import base64

    owner, stranger = make_user(), make_user()
    pid = make_project(client, owner["headers"])["id"]
    tid = make_task(client, owner["headers"], pid)["id"]
    ghost = uuid.uuid4()
    if case == "alg_none":
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        token = f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc({'sub': owner['id'], 'exp': 9999999999})}."
        assert client.delete(f"/projects/{pid}", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    elif case == "stranger_delete":
        assert client.delete(f"/projects/{pid}", headers=stranger["headers"]).status_code == 403
    elif case == "sqli_login":
        assert client.post("/login", json={"email": "' OR '1'='1", "password": "' OR '1'='1"}).status_code in (401, 422)
    elif case == "role_mass_assignment":
        assert client.post("/register", json={"email": unique_email(), "password": PASSWORD, "role": "admin"}).status_code == 422
    elif case == "expired_token":
        now = datetime.now(timezone.utc)
        token = jwt.encode({"sub": owner["id"], "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)},
                           jwt_secret, algorithm="HS256")
        assert client.get(f"/projects/{pid}/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    elif case == "unauth_create_project":
        assert client.post("/projects", json={"name": "x"}).status_code == 401
    elif case == "unauth_list":
        assert client.get(f"/projects/{pid}/tasks").status_code == 401
    elif case == "unauth_patch":
        assert client.patch(f"/tasks/{ghost}", json={"status": "done"}).status_code == 401
    elif case == "unauth_delete":
        assert client.delete(f"/projects/{ghost}").status_code == 401
    else:
        assert client.patch(f"/tasks/{tid}", json={"status": "done"}, headers=stranger["headers"]).status_code == 403


# --------------------------------- quality ---------------------------------
@pytest.mark.quality
def test_candidate_tests_reach_coverage_threshold(candidate_run):
    assert candidate_run["coverage"] is not None, candidate_run["output"]
    assert candidate_run["coverage"] >= COVERAGE_THRESHOLD, f"coverage {candidate_run['coverage']:.1f}%"


@pytest.mark.quality
def test_candidate_tests_are_real_tests():
    tree = ast.parse(_read("tests/test_e2e.py"))
    tests = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert len(tests) >= MIN_CANDIDATE_TESTS
    empty = [t.name for t in tests if not any(isinstance(n, ast.Assert) for n in ast.walk(t))]
    assert empty == [], f"tests without assertions: {empty}"


@pytest.mark.quality
def test_readme_documents_setup_api_and_testing():
    text = _read("README.md")
    low = text.lower()
    headings = " ".join(re.findall(r"^#+\s*(.+)$", text, flags=re.M)).lower()
    for needle in ("api", "test", "config", "start|setup|install|run"):
        assert re.search(needle, headings), f"no heading about {needle}"
    for path in README_PATHS:
        assert path in text, f"README does not mention {path}"
    assert "pytest" in low and "docker compose" in low and "migrate" in low


@pytest.mark.quality
def test_compose_defines_api_and_database_with_healthchecks():
    data = yaml.safe_load(_read("docker-compose.yml"))
    services = data["services"]
    assert {"api", "db"} <= set(services)
    assert "postgres" in services["db"]["image"] and "healthcheck" in services["db"]
    dep = services["api"]["depends_on"]
    assert isinstance(dep, dict) and dep["db"]["condition"] == "service_healthy"
    assert any("8000" in str(p) for p in services["api"]["ports"])


@pytest.mark.quality
def test_dockerfile_is_production_shaped():
    text = _read("Dockerfile")
    assert "HEALTHCHECK" in text and "EXPOSE" in text and "uvicorn" in text
    assert re.search(r"pip install[^\n]*--no-cache-dir|PIP_NO_CACHE_DIR", text)
    assert re.search(r"COPY\s+requirements", text), "dependencies should be installed in a cacheable layer"


@pytest.mark.quality
def test_env_example_lists_every_setting():
    keys = {ln.split("=", 1)[0].strip() for ln in _read(".env.example").splitlines() if "=" in ln and not ln.startswith("#")}
    assert set(ENV_KEYS) <= keys

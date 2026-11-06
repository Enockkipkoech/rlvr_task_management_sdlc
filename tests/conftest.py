"""Shared fixtures and the result-recording plugin for the six grader suites.

The suites test a *candidate* application tree: ``CANDIDATE_APP_DIR`` (default:
``task_api_reference``, the gold solution).  The evaluator in
``task_management_rlvr.evaluator`` builds the candidate by overlaying a model's
files on the reference tree and runs one suite file per task.

Every test carries exactly one category marker (functional | database |
security | quality); a task's category score is passed/total in that category.
Tests marked ``hidden`` are adversarial/edge cases that are never described in
the prompt.  Skipped tests count as failures.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
CATEGORIES = ("functional", "database", "security", "quality")

CANDIDATE_DIR = Path(
    os.environ.get("CANDIDATE_APP_DIR", str(PROJECT_ROOT / "task_api_reference"))
).resolve()

PARAMS = {
    "password_min_length": 8,
    "title_max_length": 120,
    "token_ttl_minutes": 30,
    **json.loads(os.environ.get("TMR_PARAMS", "{}")),
}
JWT_SECRET = os.environ.get("JWT_SECRET") or ("grader-secret-" + "k" * 40)

os.environ["JWT_SECRET"] = JWT_SECRET
os.environ["PASSWORD_MIN_LENGTH"] = str(PARAMS["password_min_length"])
os.environ["TITLE_MAX_LENGTH"] = str(PARAMS["title_max_length"])
os.environ["ACCESS_TOKEN_TTL_MINUTES"] = str(PARAMS["token_ttl_minutes"])
sys.path.insert(0, str(CANDIDATE_DIR))
sys.path.insert(0, str(TESTS_DIR))

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)


# --------------------------------------------------------------------------
# Result recording
# --------------------------------------------------------------------------
_RESULTS: dict[str, dict] = {}


def pytest_configure(config: pytest.Config) -> None:
    for name in (*CATEGORIES, "hidden"):
        config.addinivalue_line("markers", f"{name}: grader marker")


def _category(item: pytest.Item) -> str | None:
    found = [c for c in CATEGORIES if item.get_closest_marker(c)]
    return found[0] if len(found) == 1 else None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if not item.path.name.startswith("test_task_"):
            continue  # only the grader suites need category markers
        if _category(item) is None:
            raise pytest.UsageError(f"{item.nodeid}: exactly one category marker required")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    rep = outcome.get_result()
    entry = _RESULTS.setdefault(
        item.nodeid,
        {"nodeid": item.nodeid, "category": _category(item),
         "hidden": bool(item.get_closest_marker("hidden")), "passed": False},
    )
    if rep.when == "setup" and rep.outcome != "passed":
        entry["passed"] = False
        entry["detail"] = "setup " + rep.outcome
    elif rep.when == "call":
        entry["passed"] = rep.outcome == "passed"
        if rep.outcome != "passed":
            entry["detail"] = str(rep.longrepr)[-400:]
    elif rep.when == "setup" and rep.outcome == "passed":
        entry.setdefault("passed", False)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    path = os.environ.get("TMR_RESULTS_PATH")
    if path:
        Path(path).write_text(json.dumps(list(_RESULTS.values())), encoding="utf-8")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def reset_schema(url: str) -> None:
    """Return a database to a pristine, empty ``public`` schema."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(scope="session")
def params() -> dict:
    return dict(PARAMS)


@pytest.fixture(scope="session")
def jwt_secret() -> str:
    return JWT_SECRET


@pytest.fixture(scope="session")
def database_url():
    """An empty database for this session (pre-created by the evaluator, or made here)."""
    preset = os.environ.get("TMR_DATABASE_URL")
    if preset:
        reset_schema(preset)
        os.environ["DATABASE_URL"] = preset
        yield preset
        return
    name = f"tmr_grade_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(ADMIN_URL, dbname=name)
    os.environ["DATABASE_URL"] = url
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def migrated_url(database_url):
    """The session database with the candidate's migrations applied."""
    from app.migrate import run_migrations

    run_migrations(database_url)
    return database_url


@pytest.fixture()
def db(migrated_url):
    with psycopg.connect(migrated_url, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture()
def client(migrated_url):
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture(scope="session")
def _fast_hash() -> str:
    from argon2 import PasswordHasher

    return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("Correct-Horse-Battery-42")


@pytest.fixture()
def make_user(migrated_url, _fast_hash, jwt_secret):
    """Factory: insert a user row directly and mint a valid token (no hashing cost).

    Used by suites that are not about authentication so they run fast and do not
    depend on the candidate's auth code.  ``role='admin'`` creates an admin.
    """
    from datetime import datetime, timedelta, timezone

    import jwt

    from helpers import PASSWORD, unique_email

    def _make(role: str = "user", email: str | None = None) -> dict:
        email = email or unique_email()
        with psycopg.connect(migrated_url, autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute(
                "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                (email, _fast_hash, role),
            ).fetchone()
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {"sub": str(row["id"]), "iat": now, "exp": now + timedelta(minutes=30)},
            jwt_secret, algorithm="HS256",
        )
        return {"id": str(row["id"]), "email": email, "password": PASSWORD, "token": token,
                "headers": {"Authorization": f"Bearer {token}"}}

    return _make

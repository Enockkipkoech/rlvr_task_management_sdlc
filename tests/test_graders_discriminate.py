"""Mutation checks: deliberately broken implementations must score below the gold solution.

Each mutant edits the gold reference with an exact-text replacement (the test fails
loudly if the anchor text drifted) and runs the real evaluator against PostgreSQL.
Needs TEST_DATABASE_ADMIN_URL; skipped when no database is reachable.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from task_management_rlvr.evaluator import evaluate_files
from task_management_rlvr.tasks import get_task, gold_files


def _db_available() -> bool:
    url = os.environ.get("TEST_DATABASE_ADMIN_URL")
    if not url:
        return False
    try:
        psycopg.connect(url, connect_timeout=3).close()
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="PostgreSQL (TEST_DATABASE_ADMIN_URL) not reachable")

# name -> (task_id, {file: [(old, new)]}, categories that must drop below 1.0)
MUTANTS = {
    "t1_no_status_check": (1, {"migrations/0001_init.sql": [
        ("    CONSTRAINT tasks_status_valid CHECK (status IN ('todo', 'in_progress', 'done')),\n", "")]},
        ["database"]),
    "t1_no_cascade": (1, {"migrations/0001_init.sql": [("ON DELETE CASCADE", "ON DELETE NO ACTION")]},
                      ["database"]),
    "t2_plaintext_password": (2, {"app/security.py": [("return _hasher.hash(plain)", "return plain")]},
                              ["security"]),
    "t3_string_built_sql": (3, {"app/repositories/tasks.py": [(
        '''    return conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, assignee_id) "
        f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
        (str(project_id), title, description, status,
         str(assignee_id) if assignee_id is not None else None),
    ).fetchone()''',
        '''    assignee = f"'{assignee_id}'" if assignee_id is not None else "NULL"
    return conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, assignee_id) "
        f"VALUES ('{project_id}', '{title}', '{description}', '{status}', {assignee}) RETURNING {_COLUMNS}"
    ).fetchone()''')]}, ["security"]),
    "t4_no_row_locks": (4, {"app/services/project_service.py": [
        ("if projects.lock(conn, project_id, exclusive=False) is None:", "if projects.get(conn, project_id) is None:"),
        ('project = projects.lock(conn, task["project_id"], exclusive=False)', 'project = projects.get(conn, task["project_id"])'),
        ("if projects.lock(conn, project_id, exclusive=True) is None:", "if projects.get(conn, project_id) is None:")]},
        ["database"]),
    "t4_everyone_is_owner": (4, {"app/services/project_service.py": [
        ('return user["role"] == "admin" or project["owner_id"] == user["id"]', "return True")]},
        ["functional", "security"]),
    "t5_sql_injectable_users_repo": (5, {"app/repositories/users.py": [(
        '''f"SELECT {_COLUMNS} FROM users WHERE email = %s", (email,)''',
        '''f"SELECT {_COLUMNS} FROM users WHERE email = '{email}'"''')]}, ["security", "database"]),
    "t5_leaky_500": (5, {"app/errors.py": [(
        'return error_response(500, "internal_error", "Internal server error")',
        'return error_response(500, "internal_error", f"{type(exc).__name__}: {exc}")')]}, ["security"]),
    "t6_root_container_thin_docs": (6, {"Dockerfile": [("USER appuser\n", "")],
                                       "README.md": [("# Task Management API", "# API")]},
                                   ["security", "quality"]),
}


def _mutate(task_id: int, edits: dict[str, list[tuple[str, str]]]) -> dict[str, str]:
    files = gold_files(get_task(task_id))
    for path, pairs in edits.items():
        for old, new in pairs:
            assert old in files[path], f"anchor not found in {path}: {old[:60]!r}"
            files[path] = files[path].replace(old, new)
    if task_id == 6:
        files["README.md"] = "# API\n"
    return files


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_mutant_scores_below_gold(name):
    task_id, edits, must_drop = MUTANTS[name]
    result = evaluate_files(task_id, {}, _mutate(task_id, edits))
    assert result.error is None, result.error
    assert result.reward < 1.0, result.to_dict()
    for cat in must_drop:
        assert result.scores[cat] < 1.0, f"{name}: {cat} stayed at 1.0 ({result.counts[cat]})"


def test_trivial_e2e_tests_do_not_earn_task6_credit():
    files = gold_files(get_task(6))
    files["tests/test_e2e.py"] = "def test_nothing():\n    assert True\n"
    result = evaluate_files(6, {}, files)
    assert result.scores["functional"] < 1.0 and result.scores["quality"] < 1.0


def test_writing_outside_owned_files_forfeits_quality():
    files = gold_files(get_task(1))
    result = evaluate_files(1, {}, files, violations=["file not owned by this task: app/main.py"], clean_format=False)
    assert result.scores["quality"] == 0.0 and result.scores["functional"] == 1.0

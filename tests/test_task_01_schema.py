"""Task 1 grader: database schema and migrations (owned file: migrations/0001_init.sql)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import psycopg
import pytest

from conftest import CANDIDATE_DIR

pytestmark = []

TABLES = ("users", "projects", "tasks")
COLUMN_TYPES = {
    ("users", "id"): "uuid", ("users", "email"): "text", ("users", "password_hash"): "text",
    ("users", "role"): "text", ("projects", "id"): "uuid", ("projects", "name"): "text",
    ("projects", "owner_id"): "uuid", ("tasks", "id"): "uuid", ("tasks", "project_id"): "uuid",
    ("tasks", "assignee_id"): "uuid", ("tasks", "title"): "text",
    ("tasks", "description"): "text", ("tasks", "status"): "text",
}
NOT_NULL = [("users", "email"), ("users", "password_hash"), ("users", "role"),
            ("projects", "name"), ("projects", "owner_id"), ("tasks", "project_id"),
            ("tasks", "title"), ("tasks", "status")]


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _user(db, email=None, role=None):
    email = email or f"{_uid()}@example.com"
    if role is None:
        return db.execute(
            "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING *", (email, "h")
        ).fetchone()
    return db.execute(
        "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, %s) RETURNING *",
        (email, "h", role),
    ).fetchone()


def _project(db, owner_id, name="p"):
    return db.execute(
        "INSERT INTO projects (name, owner_id) VALUES (%s, %s) RETURNING *", (name, owner_id)
    ).fetchone()


def _task(db, project_id, title="t", status=None, assignee_id=None):
    if status is None:
        return db.execute(
            "INSERT INTO tasks (project_id, title, assignee_id) VALUES (%s, %s, %s) RETURNING *",
            (project_id, title, assignee_id),
        ).fetchone()
    return db.execute(
        "INSERT INTO tasks (project_id, title, status, assignee_id) VALUES (%s, %s, %s, %s) RETURNING *",
        (project_id, title, status, assignee_id),
    ).fetchone()


# ------------------------------- functional --------------------------------
@pytest.mark.functional
@pytest.mark.parametrize("table", TABLES)
def test_table_exists(db, table):
    row = db.execute("SELECT to_regclass(%s) AS r", (f"public.{table}",)).fetchone()
    assert row["r"] is not None


@pytest.mark.functional
@pytest.mark.parametrize(("table", "column"), sorted(COLUMN_TYPES))
def test_column_type(db, table, column):
    row = db.execute(
        "SELECT data_type, udt_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column),
    ).fetchone()
    assert row is not None, f"{table}.{column} missing"
    assert COLUMN_TYPES[(table, column)] in (row["data_type"], row["udt_name"])


@pytest.mark.functional
@pytest.mark.parametrize("table", TABLES)
def test_primary_key_is_id(db, table):
    rows = db.execute(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid "
        "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary",
        (f"public.{table}",),
    ).fetchall()
    assert [r["attname"] for r in rows] == ["id"]


@pytest.mark.functional
def test_defaults_generate_ids_role_and_status(db):
    user = _user(db)
    assert isinstance(user["id"], uuid.UUID) and user["role"] == "user"
    project = _project(db, user["id"])
    task = _task(db, project["id"])
    assert isinstance(task["id"], uuid.UUID)
    assert task["status"] == "todo"
    assert task["assignee_id"] is None


@pytest.mark.functional
@pytest.mark.parametrize(("table", "column"), NOT_NULL)
def test_not_null(db, table, column):
    row = db.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s", (table, column)
    ).fetchone()
    assert row and row["is_nullable"] == "NO"


@pytest.mark.functional
def test_assignee_is_nullable(db):
    row = db.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='tasks' AND column_name='assignee_id'"
    ).fetchone()
    assert row and row["is_nullable"] == "YES"


@pytest.mark.functional
@pytest.mark.hidden
def test_all_valid_statuses_and_roles_insert(db):
    owner = _user(db, role="admin")
    project = _project(db, owner["id"])
    for status in ("todo", "in_progress", "done"):
        assert _task(db, project["id"], status=status)["status"] == status


# -------------------------------- database ---------------------------------
@pytest.mark.database
def test_email_unique(db):
    email = f"{_uid()}@example.com"
    _user(db, email)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _user(db, email)


@pytest.mark.database
@pytest.mark.parametrize("status", ["blocked", "DONE", "", "in progress"])
def test_status_check_rejects_invalid(db, status):
    project = _project(db, _user(db)["id"])
    with pytest.raises(psycopg.errors.CheckViolation):
        _task(db, project["id"], status=status)


@pytest.mark.database
def test_role_check_rejects_invalid(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        _user(db, role="superuser")


@pytest.mark.database
def test_task_requires_existing_project(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _task(db, str(uuid.uuid4()))


@pytest.mark.database
def test_project_requires_existing_owner(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _project(db, str(uuid.uuid4()))


@pytest.mark.database
def test_assignee_must_exist(db):
    project = _project(db, _user(db)["id"])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _task(db, project["id"], assignee_id=str(uuid.uuid4()))


@pytest.mark.database
def test_delete_project_cascades_to_tasks(db):
    project = _project(db, _user(db)["id"])
    ids = [_task(db, project["id"], f"t{i}")["id"] for i in range(3)]
    db.execute("DELETE FROM projects WHERE id = %s", (project["id"],))
    left = db.execute("SELECT count(*) AS n FROM tasks WHERE id = ANY(%s)", (ids,)).fetchone()
    assert left["n"] == 0


@pytest.mark.database
def test_delete_assignee_sets_null(db):
    owner, assignee = _user(db), _user(db)
    task = _task(db, _project(db, owner["id"])["id"], assignee_id=assignee["id"])
    db.execute("DELETE FROM users WHERE id = %s", (assignee["id"],))
    row = db.execute("SELECT assignee_id FROM tasks WHERE id = %s", (task["id"],)).fetchone()
    assert row is not None and row["assignee_id"] is None


@pytest.mark.database
@pytest.mark.parametrize(("table", "column", "code"),
                         [("tasks", "project_id", "c"), ("tasks", "assignee_id", "n")])
def test_fk_delete_rule_in_catalog(db, table, column, code):
    row = db.execute(
        "SELECT c.confdeltype FROM pg_constraint c JOIN pg_attribute a "
        "ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "WHERE c.contype='f' AND c.conrelid = %s::regclass AND a.attname = %s",
        (f"public.{table}", column),
    ).fetchone()
    assert row is not None and row["confdeltype"] == code


@pytest.mark.database
@pytest.mark.hidden
def test_user_with_projects_cannot_be_deleted(db):
    owner = _user(db)
    _project(db, owner["id"])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("DELETE FROM users WHERE id = %s", (owner["id"],))


@pytest.mark.database
@pytest.mark.hidden
@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_title_length_check(db, title):
    project = _project(db, _user(db)["id"])
    with pytest.raises(psycopg.errors.CheckViolation):
        _task(db, project["id"], title=title)


@pytest.mark.database
@pytest.mark.hidden
def test_blank_project_name_rejected(db):
    owner = _user(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        _project(db, owner["id"], name="   ")


@pytest.mark.database
@pytest.mark.hidden
def test_title_of_exactly_200_chars_allowed(db):
    project = _project(db, _user(db)["id"])
    assert len(_task(db, project["id"], title="x" * 200)["title"]) == 200


# -------------------------------- security ---------------------------------
@pytest.mark.security
def test_email_must_be_lowercase(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        _user(db, email=f"{_uid()}@Example.COM")


@pytest.mark.security
@pytest.mark.hidden
def test_case_variants_cannot_duplicate_an_account(db):
    base = f"{_uid()}@example.com"
    _user(db, base)
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation)):
        _user(db, base.upper())


@pytest.mark.security
def test_empty_password_hash_rejected(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO users (email, password_hash) VALUES (%s, '')",
                   (f"{_uid()}@example.com",))


@pytest.mark.security
def test_no_privileges_granted_to_public(db):
    rows = db.execute(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee = 'PUBLIC' AND table_schema = 'public' "
        "AND table_name IN ('users','projects','tasks')"
    ).fetchall()
    assert rows == []


@pytest.mark.security
@pytest.mark.parametrize("pattern", [r"\bCREATE\s+ROLE\b", r"\bALTER\s+ROLE\b", r"\bDROP\s+DATABASE\b",
                                     r"\bCOPY\b[^;]*\bPROGRAM\b", r"\bSECURITY\s+DEFINER\b",
                                     r"\bGRANT\b[^;]*\bTO\s+PUBLIC\b", r"\bCREATE\s+EXTENSION\s+(?!IF NOT EXISTS\s+pgcrypto)(?!pgcrypto)"])
def test_migration_has_no_dangerous_statements(pattern):
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted((CANDIDATE_DIR / "migrations").glob("*.sql")))
    text = re.sub(r"--[^\n]*", "", text)
    assert re.search(pattern, text, flags=re.IGNORECASE) is None


# --------------------------------- quality ---------------------------------
@pytest.mark.quality
@pytest.mark.parametrize(("table", "column"),
                         [("projects", "owner_id"), ("tasks", "project_id"), ("tasks", "assignee_id")])
def test_foreign_key_columns_are_indexed(db, table, column):
    rows = db.execute(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = i.indkey[0] WHERE i.indrelid = %s::regclass", (f"public.{table}",)
    ).fetchall()
    assert column in {r["attname"] for r in rows}


@pytest.mark.quality
@pytest.mark.parametrize("table", TABLES)
def test_created_at_timestamptz(db, table):
    row = db.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name=%s AND column_name='created_at'",
        (table,),
    ).fetchone()
    assert row and row["data_type"] == "timestamp with time zone"


@pytest.mark.quality
def test_migrations_are_ordered_and_rerunnable(migrated_url):
    from app.migrate import run_migrations

    names = [p.name for p in sorted((CANDIDATE_DIR / "migrations").glob("*.sql"))]
    assert names and all(re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", n) for n in names)
    assert run_migrations(migrated_url) == []


@pytest.mark.quality
def test_constraints_are_named(db):
    rows = db.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = ANY(ARRAY['public.users'::regclass,"
        "'public.projects'::regclass,'public.tasks'::regclass]) AND contype = 'c'"
    ).fetchall()
    assert len(rows) >= 5
    assert all(not re.fullmatch(r"\w+_check\d*", r["conname"]) for r in rows)

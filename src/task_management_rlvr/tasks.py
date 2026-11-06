"""The six SDLC tasks: ownership, parameters, requirements and prompt rendering.

Each task asks the model to write a fixed set of files (its *owned files*).
Everything else in the application comes from ``task_api_reference`` (the gold
solution), so a task is scored only on what the model wrote.  Interface stubs
shown in prompts are extracted from the reference with ``ast`` so they cannot
drift from the code the graders actually run.
"""

from __future__ import annotations

import ast
import itertools
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .validators import render_artifact

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def project_root() -> Path:
    """Repository directory containing ``task_api_reference/`` and ``tests/``."""
    env = os.environ.get("TMR_PROJECT_ROOT")
    root = Path(env).resolve() if env else Path(__file__).resolve().parents[2]
    if not (root / "task_api_reference").is_dir():
        raise RuntimeError(
            f"task_api_reference/ not found under {root}. Run from a source checkout "
            "(uv sync installs the package editable) or set TMR_PROJECT_ROOT."
        )
    return root


def reference_dir() -> Path:
    return project_root() / "task_api_reference"


# --------------------------------------------------------------------------
# Parameters (the same values drive the prompt, the reference app and the graders)
# --------------------------------------------------------------------------

PARAM_SPACE: dict[str, tuple[int, ...]] = {
    "password_min_length": (8, 10, 12),
    "title_max_length": (80, 120, 200),
    "token_ttl_minutes": (15, 30, 60),
}
DEFAULT_PARAMS = {"password_min_length": 8, "title_max_length": 120, "token_ttl_minutes": 30}
FRAMINGS = ("spec", "ticket", "checklist")

CATEGORY_WEIGHTS = {"functional": 0.50, "database": 0.20, "security": 0.20, "quality": 0.10}


@dataclass(frozen=True)
class TaskSpec:
    task_id: int
    slug: str
    title: str
    objective: str
    owned_files: tuple[str, ...]
    provided_files: tuple[str, ...]
    suite: str
    param_keys: tuple[str, ...]
    requirements: str
    example: str = ""
    timeout_s: int = 300

    @property
    def owned(self) -> frozenset[str]:
        return frozenset(self.owned_files)


# --------------------------------------------------------------------------
# Shared prose
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior backend engineer working on a FastAPI + PostgreSQL service.
You are given one engineering task. Your code is executed against an isolated PostgreSQL
database and graded by hidden automated tests: functional correctness, database
invariants, security/authorization, and API/code quality. Code that merely looks right
earns nothing.

Return ONLY the complete contents of the files you were asked to write, in this exact format:

<implementation>
<file path="relative/path.py">
...entire file contents...
</file>
</implementation>

Rules:
- Write every owned file in full. Do not write any other file.
- No markdown fences, explanations or text outside the <implementation> block.
- Use only the Python standard library, FastAPI, Pydantic v2, psycopg 3, PyJWT and argon2-cffi.
- Every SQL statement must be parameterized. Never build SQL with f-strings of user data.
- Do not print, log or return secrets, password hashes, SQL text or stack traces.""".strip()

ARCHITECTURE = """Layers: routers (HTTP only) -> services (authorization, business rules, transactions)
-> repositories (parameterized SQL, no commits) -> PostgreSQL (constraints are the source of truth).
Connections are autocommit; services open transactions with `with conn.transaction():`.
Rows are dicts (psycopg dict_row). Error responses are {"error": {"code": str, "message": str}}.
Roles: owner (project.owner_id) and admin (users.role='admin') manage everything; an assignee
may read their own tasks and change only the status of a task assigned to them; strangers get
403; unknown ids get 404; unauthenticated requests get 401."""

# --------------------------------------------------------------------------
# Requirements text per task ({params} are filled per dataset row)
# --------------------------------------------------------------------------

_T1 = """Write the PostgreSQL migration `migrations/0001_init.sql` (PostgreSQL 15+; it runs in one
transaction on an empty database; `gen_random_uuid()` is built in).

users: id UUID PK default gen_random_uuid(); email TEXT NOT NULL UNIQUE; password_hash TEXT NOT NULL;
  role TEXT NOT NULL default 'user'; created_at TIMESTAMPTZ NOT NULL default now().
  Invariants: email must equal lower(email); email not blank; password_hash must be non-empty;
  role IN ('user','admin').
projects: id UUID PK default gen_random_uuid(); name TEXT NOT NULL (not blank after btrim);
  owner_id UUID NOT NULL REFERENCES users(id) with NO cascade (deleting a user who owns projects
  must fail: ON DELETE RESTRICT); created_at TIMESTAMPTZ NOT NULL default now().
tasks: id UUID PK default gen_random_uuid(); project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE;
  assignee_id UUID NULL REFERENCES users(id) ON DELETE SET NULL; title TEXT NOT NULL with
  char_length(btrim(title)) BETWEEN 1 AND 200; description TEXT NOT NULL default '';
  status TEXT NOT NULL default 'todo' CHECK status IN ('todo','in_progress','done');
  created_at and updated_at TIMESTAMPTZ NOT NULL default now().
Also: index each foreign-key column (projects.owner_id, tasks.project_id, tasks.assignee_id);
give every constraint an explicit name (CONSTRAINT <table>_<what>); do not GRANT anything,
create roles, extensions or functions, and do not use SECURITY DEFINER. Enforce invariants in
the database, not only in the application."""

_T2 = """Implement authentication. Settings come from `app.config.get_settings()`
(access_token_ttl_minutes is {token_ttl_minutes} for this task; password length rules are enforced
by the provided request schemas, minimum {password_min_length}).

app/security.py: Argon2id (argon2-cffi) `hash_password`, `verify_password` (returns False for ANY
bad input, never raises), `create_access_token(user_id, ttl_minutes=None)` -> HS256 JWT with claims
sub, iat, exp (= iat + ttl minutes), jti, `decode_access_token(token)` -> subject string. The decoder
must pin algorithms to the configured one, require exp/iat/sub, and raise
`AppError(401, "invalid_token", ..., headers={{"WWW-Authenticate": "Bearer"}})` for malformed,
tampered, expired, unsigned, wrong-secret or wrong-algorithm tokens.
app/services/auth_service.py: `register_user(conn, email, password)` creates a role='user' account
inside `conn.transaction()`; duplicate email -> `AppError(409, "email_taken", ...)` (rely on the
UNIQUE constraint, catch psycopg UniqueViolation, so concurrent duplicates yield exactly one 201).
`authenticate(conn, email, password)` returns {{"access_token", "token_type": "bearer", "expires_in": ttl*60}};
unknown email and wrong password must produce the SAME 401 `invalid_credentials` response
(`WWW-Authenticate: Bearer`) and similar timing (verify against a dummy hash for unknown emails).
app/routers/auth.py: `router = APIRouter(...)` with POST /register (201, UserOut) and POST /login (200, TokenOut)."""

_T3 = """Implement the persistence layer and HTTP routes for projects and tasks. Authorization and business
rules live in the provided service (`app.services.project_service`); your routers call it and your
repositories are called by it.

Repositories (exact signatures as shown): every statement parameterized; `conn.execute(...).fetchone()/fetchall()`
return dict rows; repositories never commit. `tasks.update` builds its SET clause only from a whitelist of
column names ({{title, description, status, assignee_id}}) using psycopg.sql.Identifier, always sets
updated_at = now(), and returns the row (or None). `tasks.list_for_project` orders by created_at, id.
`projects.lock(..., exclusive=True)` is `SELECT ... FOR UPDATE`, `exclusive=False` is `FOR SHARE`.
Routes (use `Depends(get_current_user)` and `Depends(get_conn)`; path ids are `uuid.UUID`):
POST /projects -> 201 ProjectOut; POST /projects/{{project_id}}/tasks -> 201 TaskOut;
GET /projects/{{project_id}}/tasks -> 200 list[TaskOut]; PATCH /tasks/{{task_id}} -> 200 TaskOut (body is
TaskUpdate; pass `body.changes()`); DELETE /projects/{{project_id}} -> 204 with an empty body.
Routers stay thin: no SQL, no authorization logic."""

_T4 = """Implement `app/services/project_service.py`: authorization, business rules and transactions.
Use the provided repositories; raise `AppError` (no FastAPI imports).

Policy: owner or admin = full control. Assignee of a task = may list only THEIR tasks of that project
and change ONLY `status` of THEIR task (any other field, or a task assigned to someone else -> 403).
Anyone else -> 403. Missing project -> 404 `project_not_found`; missing task -> 404 `task_not_found`;
non-existent assignee id -> 422 `assignee_not_found`; create_task is owner/admin only.
Business rule: a project that has any task with status 'in_progress' cannot be deleted -> 409
`project_has_in_progress_tasks`. The check and the delete must happen in ONE transaction under an
exclusive row lock on the project, and every writer that can create or move a task into 'in_progress'
(create_task, update_task) must take the conflicting shared lock on the project row inside its own
transaction, so a concurrent writer can never slip an in_progress task into a project that is being
deleted (the loser gets 404/409, never a silently destroyed in_progress task). Read the role from the
user row passed in; never trust anything else. Error bodies must not echo project names, titles or ids
of resources the caller may not access."""

_T5 = """Implement validation, consistent errors and safe SQL (title max {title_max_length}, password min {password_min_length}).

app/schemas.py (Pydantic v2): every request model rejects unknown fields (extra="forbid" -> mass-assignment
safe). RegisterRequest: email (EmailStr, stripped + lower-cased), password ({password_min_length}..128 chars, no NUL).
LoginRequest: email (plain str, stripped, lower-cased, 1..254, no NUL), password (1..128, no NUL).
ProjectCreate.name: stripped, 1..100 chars. TaskCreate: title stripped 1..{title_max_length}; description stripped
<= 5000 (default ""); status in todo|in_progress|done (default todo); assignee_id UUID|None.
TaskUpdate: all optional, at least one field required, title/description/status may not be null,
assignee_id may be null (unassign); `changes()` returns only the fields the client sent. Reject control
characters (all C0 except TAB and LF, plus DEL, especially NUL) in name/title/description with a validation
error, never a 500. Also define UserOut, TokenOut, ProjectOut, TaskOut as shown in the stub.
app/errors.py: `AppError`, `error_response`, `install_exception_handlers(app)`; every failure is
{{"error": {{"code", "message"}}}}: AppError -> its status/code/message/headers; validation -> 422
`validation_error` whose message lists only the offending FIELD NAMES (never submitted values); 404/405
-> `not_found`/`method_not_allowed`; any unhandled Exception -> 500 `internal_error`, generic message,
logged server-side, nothing leaked.
app/repositories/users.py: parameterized SQL only (no f-strings/%/format with data; a module-level
UPPER_CASE column-list constant may be interpolated)."""

_T6 = """Make the service complete and runnable.
app/main.py: `create_app()` builds an independent FastAPI app (title "Task Management API"), installs the
exception handlers, includes the three routers, and adds GET /health: 200 {{"status": "ok"}} after a
`SELECT 1`, otherwise 503 with error code `database_unavailable` (use `error_response`). Export `app = create_app()`.
tests/test_e2e.py: self-contained pytest module (>= 10 tests, each with real assertions) that exercises register,
login, project/task CRUD, authorization (stranger 403, assignee limits), the 409 delete rule, validation, 401s and
SQL-injection payloads. Fixture: use `os.environ["DATABASE_URL"]` (already migrated, empty database) and a
module-level JWT secret from the environment; build `TestClient(create_app())`. The grader runs it with
coverage and requires all tests to pass and >= 80% line coverage of the `app` package.
Dockerfile: pinned base image tag (no :latest), deps from requirements.txt in a cacheable layer with
--no-cache-dir, copy app/ and migrations/, run as a NON-ROOT USER, EXPOSE 8000, HEALTHCHECK, CMD that runs
`python -m app.migrate` then uvicorn app.main:app. No secrets in ENV/ARG, never COPY .env.
docker-compose.yml: services `db` (postgres image with a healthcheck) and `api` (build ., depends_on db with
condition service_healthy, port 8000). JWT_SECRET and POSTGRES_PASSWORD come from `${{...}}` substitution, never literals.
.env.example: keys DATABASE_URL, JWT_SECRET, ACCESS_TOKEN_TTL_MINUTES, PASSWORD_MIN_LENGTH, TITLE_MAX_LENGTH
(JWT_SECRET empty or an obvious placeholder). README.md: headings for quick start/setup, configuration, API
(list all 7 endpoints: /register, /login, /projects, /projects/{{id}}/tasks, /tasks/{{id}}), testing and
architecture; mention `docker compose`, `python -m app.migrate` and `pytest`."""

_EX = {
    1: "Example constraint style: CONSTRAINT tasks_status_valid CHECK (status IN ('todo','in_progress','done')).",
    2: 'POST /login -> 200 {"access_token": "<jwt>", "token_type": "bearer", "expires_in": ttl_minutes*60}; wrong password -> 401 {"error": {"code": "invalid_credentials", "message": "..."}}.',
    3: "DELETE /projects/{id} -> 204 (empty body). PATCH /tasks/{id} with {\"assignee_id\": null} unassigns the task.",
    4: "Assignee PATCH {\"status\": \"done\"} on their own task -> 200; PATCH {\"title\": \"x\"} -> 403; stranger anything -> 403; DELETE with an in_progress task -> 409.",
    5: 'POST /register {"email": "a@b.com", "password": "short", "role": "admin"} -> 422 {"error": {"code": "validation_error", "message": "Invalid request: ..."}} (message names fields only).',
    6: "GET /health -> 200 {\"status\": \"ok\"}; with the database down -> 503 {\"error\": {\"code\": \"database_unavailable\", ...}}.",
}

TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(1, "schema", "Database schema and migrations",
             "Implement PostgreSQL tables, constraints, foreign keys and the migration.",
             ("migrations/0001_init.sql",), (), "tests/test_task_01_schema.py", (), _T1, _EX[1], 120),
    TaskSpec(2, "auth", "Authentication and JWT",
             "Implement registration, login, secure password hashing and JWT verification.",
             ("app/security.py", "app/services/auth_service.py", "app/routers/auth.py"),
             ("app/config.py", "app/errors.py", "app/db.py", "app/schemas.py", "app/repositories/users.py"),
             "tests/test_task_02_auth.py", ("token_ttl_minutes", "password_min_length"), _T2, _EX[2], 300),
    TaskSpec(3, "crud", "Project and task CRUD",
             "Implement repositories and routes for project creation, task creation/listing/update and project deletion.",
             ("app/repositories/projects.py", "app/repositories/tasks.py", "app/routers/projects.py",
              "app/routers/tasks.py"),
             ("app/db.py", "app/deps.py", "app/errors.py", "app/schemas.py", "app/services/project_service.py"),
             "tests/test_task_03_crud.py", (), _T3, _EX[3], 300),
    TaskSpec(4, "authorization", "Business rules and authorization",
             "Enforce owner/assignee/stranger permissions and the in-progress deletion rule, race-free.",
             ("app/services/project_service.py",),
             ("app/errors.py", "app/repositories/projects.py", "app/repositories/tasks.py",
              "app/repositories/users.py"),
             "tests/test_task_04_authorization.py", (), _T4, _EX[4], 300),
    TaskSpec(5, "validation", "Validation, error handling and SQL safety",
             "Implement Pydantic validation, consistent errors, input sanitization and safe user SQL.",
             ("app/schemas.py", "app/errors.py", "app/repositories/users.py"),
             ("app/config.py", "app/db.py", "app/security.py"),
             "tests/test_task_05_security.py", ("password_min_length", "title_max_length"), _T5, _EX[5], 300),
    TaskSpec(6, "e2e", "End-to-end integration and quality",
             "Deliver a runnable API: app entrypoint, automated tests, container and documentation.",
             ("app/main.py", "tests/test_e2e.py", "Dockerfile", "docker-compose.yml", ".env.example", "README.md"),
             ("app/config.py", "app/db.py", "app/errors.py", "app/migrate.py", "app/routers/auth.py",
              "app/routers/projects.py", "app/routers/tasks.py"),
             "tests/test_task_06_e2e.py", (), _T6, _EX[6], 600),
)
TASKS_BY_ID = {t.task_id: t for t in TASKS}


def get_task(task_id: int) -> TaskSpec:
    try:
        return TASKS_BY_ID[int(task_id)]
    except (KeyError, ValueError):
        raise KeyError(f"unknown task id {task_id!r}; valid: {sorted(TASKS_BY_ID)}") from None


# --------------------------------------------------------------------------
# Interface stubs extracted from the reference with ast
# --------------------------------------------------------------------------


def _doc(node: ast.AST, indent: str) -> list[str]:
    doc = ast.get_docstring(node)  # type: ignore[arg-type]
    if not doc:
        return []
    body = "\n".join(indent + ln if ln else "" for ln in doc.splitlines())
    return [f'{indent}"""{body.strip()}"""'] if "\n" not in doc else [f'{indent}"""', body, f'{indent}"""']


def _func_stub(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str = "") -> list[str]:
    out = [f"{indent}@{ast.unparse(d)}" for d in node.decorator_list]
    kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    out.append(f"{indent}{kw} {node.name}({ast.unparse(node.args)}){ret}:")
    out += _doc(node, indent + "    ")
    out.append(f"{indent}    ...")
    return out


def render_stub(rel_path: str, root: Path | None = None) -> str:
    """Public API of a reference module: signatures, fields and docstrings, no bodies."""
    src = ((root or reference_dir()) / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = [f"# {rel_path}"]
    out += _doc(tree, "")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            out += _func_stub(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            out.append(f"class {node.name}({bases}):" if bases else f"class {node.name}:")
            out += _doc(node, "    ")
            for item in node.body:
                if isinstance(item, ast.AnnAssign):
                    out.append("    " + ast.unparse(item))
                elif isinstance(item, ast.Assign) and not isinstance(item.value, ast.Constant):
                    out.append("    " + ast.unparse(item))
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        not item.name.startswith("_") or item.name == "__init__"):
                    out += _func_stub(item, "    ")
            out.append("")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if names and not names[0].startswith("_") and not names[0].isupper():
                out.append(ast.unparse(node)[:160])
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------


def relevant_params(spec: TaskSpec, params: dict[str, Any]) -> dict[str, int]:
    merged = {**DEFAULT_PARAMS, **(params or {})}
    return {k: int(merged[k]) for k in spec.param_keys}


def build_prompt(spec: TaskSpec, params: dict[str, Any], framing: str = "spec", with_examples: bool = True) -> str:
    """Render the user prompt for a task. Same information in every framing."""
    values = {**DEFAULT_PARAMS, **(params or {})}
    requirements = spec.requirements.format(**values)
    required = "\n".join(render_stub(p) for p in spec.owned_files if p.endswith(".py"))
    provided = "\n".join(render_stub(p) for p in spec.provided_files)
    files = "\n".join(f"- {p}" for p in spec.owned_files)
    example = f"\nExample:\n{spec.example}\n" if with_examples and spec.example else ""
    tail = (f"\nFiles to write (complete contents, nothing else):\n{files}\n\n"
            "Reply with a single <implementation> block.")
    stubs = ""
    if required:
        stubs += f"\nRequired exports (your files must keep these names and signatures; bodies are yours):\n```python\n{required}```\n"
    if provided:
        stubs += f"\nProvided modules you may import (already written, do not rewrite):\n```python\n{provided}```\n"

    if framing == "ticket":
        return (f"TICKET TSK-{spec.task_id}: {spec.title}\n\nAs the backend engineer on this service, {spec.objective.lower()}\n\n"
                f"Acceptance criteria:\n{requirements}\n{example}\nContext:\n{ARCHITECTURE}\n{stubs}{tail}")
    if framing == "checklist":
        return (f"{spec.title.upper()} - implement and satisfy every item.\n\n{requirements}\n{example}{stubs}{tail}")
    return (f"# Task {spec.task_id}: {spec.title}\n\nObjective: {spec.objective}\n\n## Architecture\n{ARCHITECTURE}\n\n"
            f"## Requirements\n{requirements}\n{example}{stubs}{tail}")


# --------------------------------------------------------------------------
# Dataset rows
# --------------------------------------------------------------------------


def param_combinations(spec: TaskSpec) -> list[dict[str, int]]:
    if not spec.param_keys:
        return [{}]
    values = [PARAM_SPACE[k] for k in spec.param_keys]
    return [dict(zip(spec.param_keys, combo)) for combo in itertools.product(*values)]


def all_rows() -> list[dict[str, Any]]:
    """One row per (task, parameter combination, framing, examples flag)."""
    rows = []
    for spec in TASKS:
        for params in param_combinations(spec):
            for framing in FRAMINGS:
                for with_examples in (True, False):
                    key = f"t{spec.task_id}-" + "-".join(f"{k}={v}" for k, v in sorted(params.items()))
                    rows.append({"id": f"{key}-{framing}-{'ex' if with_examples else 'noex'}",
                                 "task_id": spec.task_id, "params": params,
                                 "framing": framing, "with_examples": with_examples})
    return rows


# --------------------------------------------------------------------------
# Gold reply (reference files rendered as an artifact)
# --------------------------------------------------------------------------


def gold_files(spec: TaskSpec) -> dict[str, str]:
    ref = reference_dir()
    return {p: (ref / p).read_text(encoding="utf-8") for p in spec.owned_files}


def gold_reply(spec: TaskSpec) -> str:
    return render_artifact(gold_files(spec))

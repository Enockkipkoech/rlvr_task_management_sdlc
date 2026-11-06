# Task Management API

A secure, layered REST API for projects and tasks: FastAPI, PostgreSQL, JWT.

## Architecture

Routers (HTTP) -> services (authorization, business rules, transactions) ->
repositories (parameterized SQL) -> PostgreSQL (constraints are the source of truth).

## Quick start

    cp .env.example .env        # set JWT_SECRET and POSTGRES_PASSWORD
    docker compose up --build   # API on http://localhost:8000, docs at /docs

Without Docker:

    pip install -r requirements.txt
    export DATABASE_URL=postgresql://... JWT_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
    python -m app.migrate
    uvicorn app.main:app

## Configuration

DATABASE_URL, JWT_SECRET (32+ chars), ACCESS_TOKEN_TTL_MINUTES, PASSWORD_MIN_LENGTH,
TITLE_MAX_LENGTH.  See `.env.example`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | /register | Register a user (409 on duplicate email) |
| POST | /login | Exchange credentials for a bearer JWT |
| POST | /projects | Create a project |
| POST | /projects/{id}/tasks | Create a task (owner) |
| GET | /projects/{id}/tasks | List tasks (owner: all, assignee: own) |
| PATCH | /tasks/{id} | Update a task (assignee: status only) |
| DELETE | /projects/{id} | Delete a project (409 while a task is in_progress) |
| GET | /health | Liveness and database check |

Errors always look like `{"error": {"code": "...", "message": "..."}}`.

## Testing

    pip install -r requirements-dev.txt
    export TEST_DATABASE_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres
    pytest tests --cov=app

## Security notes

Argon2id password hashes, HS256 JWTs with required `exp`, role read from the
database (never from the token), parameterized SQL only, no internals in errors.

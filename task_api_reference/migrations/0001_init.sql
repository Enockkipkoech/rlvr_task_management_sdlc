-- 0001_init.sql: Task Management API schema.
-- Invariants live in PostgreSQL, not only in the application layer.

CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_email_unique UNIQUE (email),
    CONSTRAINT users_email_lowercase CHECK (email = lower(email)),
    CONSTRAINT users_email_not_blank CHECK (char_length(btrim(email)) > 0),
    CONSTRAINT users_password_hash_not_empty CHECK (char_length(password_hash) > 0),
    CONSTRAINT users_role_valid CHECK (role IN ('user', 'admin'))
);

CREATE TABLE projects (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    owner_id   UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT projects_name_not_blank CHECK (char_length(btrim(name)) > 0)
);

CREATE TABLE tasks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    assignee_id UUID REFERENCES users (id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'todo',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tasks_status_valid CHECK (status IN ('todo', 'in_progress', 'done')),
    CONSTRAINT tasks_title_length CHECK (char_length(btrim(title)) BETWEEN 1 AND 200)
);

CREATE INDEX idx_projects_owner_id   ON projects (owner_id);
CREATE INDEX idx_tasks_project_id    ON tasks (project_id);
CREATE INDEX idx_tasks_assignee_id   ON tasks (assignee_id);

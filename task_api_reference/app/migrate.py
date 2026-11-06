"""Minimal forward-only SQL migration runner.

Applies ``migrations/*.sql`` in filename order, each in its own transaction,
and records them in ``schema_migrations``.  Usage: ``python -m app.migrate``.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def run_migrations(database_url: str | None = None, directory: Path | None = None) -> list[str]:
    url = database_url or get_settings().database_url
    folder = directory or MIGRATIONS_DIR
    applied_now: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(folder.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now


if __name__ == "__main__":
    for name in run_migrations():
        print(f"applied {name}")
    print("migrations up to date")

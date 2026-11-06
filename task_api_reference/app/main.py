"""Application factory."""

from __future__ import annotations

from fastapi import FastAPI

from app.db import connect
from app.errors import error_response, install_exception_handlers
from app.routers import auth, projects, tasks


def create_app() -> FastAPI:
    app = FastAPI(title="Task Management API", version="1.0.0")
    install_exception_handlers(app)
    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(tasks.router)

    @app.get("/health", tags=["ops"])
    def health():
        try:
            with connect() as conn:
                conn.execute("SELECT 1")
        except Exception:
            return error_response(503, "database_unavailable", "Database unavailable")
        return {"status": "ok"}

    return app


app = create_app()

"""Execute a model's implementation artifact against an isolated PostgreSQL database.

Pipeline for one rollout:
  1. parse + validate the artifact (validators.py);
  2. build a sandbox directory = reference tree minus the task's owned files
     (and minus the reference tests) plus the model's files;
  3. provision a throw-away role + database (the grader process never receives
     admin credentials);
  4. run the task's grader suite (tests/test_task_0X_*.py) in a subprocess with a
     minimal environment, a timeout and resource limits;
  5. score each category as passed/total from per-test results.

SECURITY: step 4 executes model-written code.  Resource limits and a scrubbed
environment reduce, but do not remove, the risk.  For real training runs execute
the whole evaluator inside a container/VM without network egress or credentials.
"""

from __future__ import annotations

import json
import os
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from .tasks import CATEGORY_WEIGHTS, TaskSpec, get_task, project_root, reference_dir
from .validators import ParsedArtifact, parse_artifact

CATEGORIES = tuple(CATEGORY_WEIGHTS)
_COPY_EXCLUDE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests", ".coverage")
_SEMAPHORE = threading.BoundedSemaphore(int(os.environ.get("TMR_MAX_PARALLEL_EVALS", "4")))


class EvaluatorConfigError(RuntimeError):
    """Infrastructure problem (e.g. no database): raised, never turned into a zero reward."""


@dataclass
class EvalResult:
    scores: dict[str, float] = field(default_factory=lambda: {c: 0.0 for c in CATEGORIES})
    counts: dict[str, tuple[int, int]] = field(default_factory=lambda: {c: (0, 0) for c in CATEGORIES})
    clean_format: bool = False
    violations: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0

    @property
    def reward(self) -> float:
        return sum(CATEGORY_WEIGHTS[c] * self.scores[c] for c in CATEGORIES)

    def to_dict(self) -> dict[str, Any]:
        return {"reward": round(self.reward, 4), "scores": self.scores, "counts": self.counts,
                "clean_format": self.clean_format, "violations": self.violations,
                "failed": self.failed[:25], "error": self.error, "duration_s": round(self.duration_s, 1)}


def admin_url() -> str:
    url = os.environ.get("TEST_DATABASE_ADMIN_URL", "").strip()
    if not url:
        raise EvaluatorConfigError(
            "TEST_DATABASE_ADMIN_URL is not set. Point it at a PostgreSQL server where the "
            "role may CREATE DATABASE and CREATE ROLE (see .env.example).")
    return url


# --------------------------------------------------------------------------
# Sandbox and database
# --------------------------------------------------------------------------
def build_sandbox(spec: TaskSpec, files: dict[str, str], parent: Path) -> Path:
    sandbox = parent / "candidate"
    shutil.copytree(reference_dir(), sandbox, ignore=_COPY_EXCLUDE)
    for rel in spec.owned_files:
        (sandbox / rel).unlink(missing_ok=True)
    for rel, content in files.items():
        target = sandbox / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for pkg in {Path(rel).parent for rel in files if rel.endswith(".py") and Path(rel).parent != Path(".")}:
        init = sandbox / pkg / "__init__.py"
        if not init.exists() and str(pkg).startswith("app"):
            init.write_text("", encoding="utf-8")
    return sandbox


class TempDatabase:
    """A database + login role that exist only for one evaluation."""

    def __init__(self) -> None:
        self.admin = admin_url()
        self.name = "tmr_" + uuid.uuid4().hex[:16]
        self.password = secrets.token_hex(16)
        self.url = ""

    def __enter__(self) -> "TempDatabase":
        try:
            with psycopg.connect(self.admin, autocommit=True, connect_timeout=10) as c:
                c.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(self.name), sql.Literal(self.password)))
                c.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(self.name), sql.Identifier(self.name)))
        except psycopg.Error as exc:
            raise EvaluatorConfigError(f"cannot provision test database: {exc}") from exc
        self.url = make_conninfo(self.admin, dbname=self.name, user=self.name, password=self.password)
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            with psycopg.connect(self.admin, autocommit=True, connect_timeout=10) as c:
                c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.name)))
                c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(self.name)))
        except psycopg.Error:
            pass


def _limits() -> None:  # runs in the child before exec
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 2**20, 256 * 2**20))


def _scrubbed_env(sandbox: Path, **extra: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(sandbox), "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    env.update(extra)
    return env


def _run_suite(spec: TaskSpec, sandbox: Path, params: dict[str, Any], db_url: str, results: Path) -> str:
    root = project_root()
    cmd = [sys.executable, "-m", "pytest", str(root / spec.suite), "-q", "-p", "no:cacheprovider",
           "--tb=no", "-W", "ignore", f"--rootdir={root / 'tests'}", f"--basetemp={sandbox / '.pt'}"]
    env = _scrubbed_env(
        sandbox, CANDIDATE_APP_DIR=str(sandbox), TMR_PARAMS=json.dumps(params), TMR_DATABASE_URL=db_url,
        TMR_RESULTS_PATH=str(results), JWT_SECRET="eval-" + secrets.token_urlsafe(40))
    proc = subprocess.Popen(cmd, cwd=sandbox, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, preexec_fn=_limits, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return f"timeout after {spec.timeout_s}s\n{out[-600:]}"
    return out[-1200:]


def _score(rows: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, tuple[int, int]], list[str]]:
    scores, counts, failed = {}, {}, []
    for cat in CATEGORIES:
        tests = [r for r in rows if r["category"] == cat]
        passed = sum(1 for r in tests if r["passed"])
        counts[cat] = (passed, len(tests))
        scores[cat] = passed / len(tests) if tests else 0.0
    failed = [r["nodeid"].split("::", 1)[-1] for r in rows if not r["passed"]]
    return scores, counts, failed


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def evaluate_files(task_id: int, params: dict[str, Any], files: dict[str, str],
                   violations: list[str] | None = None, clean_format: bool = True) -> EvalResult:
    spec = get_task(task_id)
    result = EvalResult(clean_format=clean_format, violations=list(violations or []))
    started = time.monotonic()
    with _SEMAPHORE, tempfile.TemporaryDirectory(prefix="tmr_eval_") as tmp, TempDatabase() as db:
        sandbox = build_sandbox(spec, files, Path(tmp))
        results_path = Path(tmp) / "results.json"
        tail = _run_suite(spec, sandbox, params, db.url, results_path)
        if not results_path.is_file():
            last = tail.strip().splitlines()[-1] if tail.strip() else "no output"
            result.error = f"grader produced no results: {last}"
        else:
            rows = json.loads(results_path.read_text())
            result.scores, result.counts, result.failed = _score(rows)
    if result.violations:  # writing outside the owned files forfeits the quality score
        result.scores["quality"] = 0.0
    result.duration_s = time.monotonic() - started
    return result


def evaluate_reply(task_id: int, params: dict[str, Any], completion: Any) -> EvalResult:
    """Score a raw model reply (string or message list) for one task."""
    spec = get_task(task_id)
    artifact: ParsedArtifact = parse_artifact(completion, spec.owned)
    if not artifact.usable:
        return EvalResult(violations=artifact.violations, error="no usable <implementation> files")
    return evaluate_files(task_id, params, artifact.files, artifact.violations, artifact.clean)

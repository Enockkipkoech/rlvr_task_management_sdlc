#!/usr/bin/env python
"""Check the local setup.  Add --gold to also score the gold solution on all six tasks (expects 1.0)."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

REQUIRED = ["verifiers.v1", "fastapi", "psycopg", "pydantic", "jwt", "argon2", "pytest", "pytest_cov", "yaml",
            "task_management_rlvr"]


def check_imports() -> bool:
    print("\n[1/4] Python imports")
    ok = True
    for name in REQUIRED:
        try:
            importlib.import_module(name)
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  FAIL  {name}: {exc}")
    return ok


def check_database() -> bool:
    print("\n[2/4] PostgreSQL (evaluator provisioning)")
    from task_management_rlvr.evaluator import EvaluatorConfigError, TempDatabase

    print(f"  Python:  {sys.version.split()[0]}  Project: {ROOT}")
    try:
        with TempDatabase() as db:
            import psycopg

            with psycopg.connect(db.url, autocommit=True) as conn:
                conn.execute("DROP SCHEMA public CASCADE")
                conn.execute("CREATE SCHEMA public")
                version = conn.execute("SHOW server_version").fetchone()[0]
        print(f"  PASS  created/dropped a role-scoped database (PostgreSQL {version})")
        return True
    except EvaluatorConfigError as exc:
        print(f"  FAIL  {exc}")
        return False


def check_taskset() -> bool:
    print("\n[3/4] Taskset")
    from task_management_rlvr.environment import TaskManagementTaskset, TaskManagementTasksetConfig

    try:
        rows = {s: list(TaskManagementTaskset(TaskManagementTasksetConfig(split=s))) for s in ("train", "validation")}
        print(f"  PASS  train={len(rows['train'])} validation={len(rows['validation'])} tasks")
        print(f"  Rewards: {[f.__name__ for f in rows['train'][0].hooks('reward')]}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {type(exc).__name__}: {exc}")
        return False


def check_gold() -> bool:
    print("\n[4/4] Gold solution through the real evaluator")
    from task_management_rlvr import tasks
    from task_management_rlvr.evaluator import evaluate_reply

    ok = True
    for spec in tasks.TASKS:
        res = evaluate_reply(spec.task_id, tasks.DEFAULT_PARAMS, tasks.gold_reply(spec))
        good = res.reward == 1.0
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  task {spec.task_id} {spec.slug:<14} reward={res.reward:.3f} "
              f"({res.duration_s:.0f}s) {res.failed[:3] if not good else ''} {res.error or ''}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", action="store_true", help="score the gold solution on every task")
    args = parser.parse_args()
    print("=" * 65 + "\nTASK MANAGEMENT RLVR ENVIRONMENT CHECK\n" + "=" * 65)
    results = [check_imports()]
    if results[0]:
        results.append(check_database())
        results.append(check_taskset())
        if args.gold and results[1]:
            results.append(check_gold())
    print("\n" + "=" * 65 + f"\nRESULT: {'PASS' if all(results) else 'FAIL'}\n" + "=" * 65)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

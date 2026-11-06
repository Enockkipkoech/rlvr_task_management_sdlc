#!/usr/bin/env python
"""Score one model reply file against a task:  python scripts/score_reply.py --task 3 reply.txt"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from task_management_rlvr.evaluator import evaluate_reply  # noqa: E402
from task_management_rlvr.tasks import DEFAULT_PARAMS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--params", default="{}", help='JSON, e.g. \'{"password_min_length": 10}\'')
    ap.add_argument("reply", type=Path)
    args = ap.parse_args()
    res = evaluate_reply(args.task, {**DEFAULT_PARAMS, **json.loads(args.params)}, args.reply.read_text())
    print(json.dumps(res.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

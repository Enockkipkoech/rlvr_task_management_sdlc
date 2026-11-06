#!/usr/bin/env python
"""Write the deterministic dataset to data/{train,validation}.jsonl and print statistics.

The taskset builds the same rows in memory; this export is for inspection, auditing
prompts, or feeding other trainers.  No network access.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from task_management_rlvr.dataset import load_rows, render_row, write_jsonl  # noqa: E402


def main() -> int:
    out = ROOT / "data"
    for split in ("train", "validation"):
        rows = load_rows(split)
        write_jsonl(out / f"{split}.jsonl", rows)
        per_task = collections.Counter(r["task_id"] for r in rows)
        sizes = [len(render_row(r)["prompt"]) for r in rows]
        print(f"{split:<11} rows={len(rows):<4} per task={dict(sorted(per_task.items()))} "
              f"prompt chars min/avg/max={min(sizes)}/{sum(sizes)//len(sizes)}/{max(sizes)}")
    print(f"\nwrote {out}/train.jsonl and {out}/validation.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

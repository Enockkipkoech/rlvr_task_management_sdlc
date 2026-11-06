"""Deterministic dataset of RLVR task rows (no network, no downloads).

One row = (task, parameter combination, prompt framing, examples flag).  The
parameters (password length, title length, token TTL) change both the prompt and
the grader, so rows are genuinely different verifiable tasks, not paraphrases.
Evaluation rows are held out per task (every 5th row of the sorted rows), which
for tasks 2 and 5 means unseen parameter combinations.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .tasks import SYSTEM_PROMPT, all_rows, build_prompt, get_task


def split_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    train, held = [], []
    for task_id in sorted({r["task_id"] for r in rows}):
        mine = sorted((r for r in rows if r["task_id"] == task_id), key=lambda r: r["id"])
        for i, row in enumerate(mine):
            (held if i % 5 == 4 else train).append(row)
    return train, held


def balance_by_task(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repeat the rows of smaller tasks (ids get a ``#k`` suffix) so every task has equal weight.

    Tasks 2 and 5 have many parameter variants; without this, uniform sampling would be
    ~80% auth/validation.  Repeats are fine for RL: each visit draws fresh rollouts.
    """
    by_task: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)
    target = max(len(v) for v in by_task.values()) if by_task else 0
    out: list[dict[str, Any]] = []
    for task_id in sorted(by_task):
        mine = by_task[task_id]
        for i in range(target):
            row = mine[i % len(mine)]
            out.append(row if i < len(mine) else {**row, "id": f"{row['id']}#{i // len(mine) + 1}"})
    return out


def load_rows(split: str = "train", task_ids: list[int] | None = None, num_samples: int = -1,
              seed: int = 42, balance: bool = True) -> list[dict[str, Any]]:
    if split not in {"train", "validation"}:
        raise ValueError("split must be 'train' or 'validation'")
    if num_samples < -1:
        raise ValueError("num_samples must be -1 or greater")
    train, held = split_rows(all_rows())
    rows = train if split == "train" else held
    if task_ids:
        for tid in task_ids:
            get_task(tid)  # validates
        rows = [r for r in rows if r["task_id"] in set(task_ids)]
    rows = sorted(rows, key=lambda r: r["id"])
    if balance and split == "train" and rows:
        rows = balance_by_task(rows)
    if num_samples != -1:
        random.Random(seed).shuffle(rows)
        rows = rows[: min(num_samples, len(rows))]
    return rows


def render_row(row: dict[str, Any]) -> dict[str, Any]:
    spec = get_task(row["task_id"])
    return {**row, "system_prompt": SYSTEM_PROMPT,
            "prompt": build_prompt(spec, row["params"], row["framing"], row["with_examples"]),
            "owned_files": list(spec.owned_files)}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(render_row(row), ensure_ascii=False) + "\n")

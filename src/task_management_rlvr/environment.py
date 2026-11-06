"""Verifiers v1 environment: six SDLC tasks, one taskset, executable weighted rewards.

Mirrors the conventions of the sibling ``pii_masking`` environment
(Task / Taskset / TaskData, ``@vf.reward``).  Rewards run the model's code
against an isolated PostgreSQL database; see evaluator.py.

Reward weights (sum to 1.0, per the case-study design):
    functional 0.50 | database invariants 0.20 | security + authorization 0.20 | quality 0.10
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import verifiers.v1 as vf

from .dataset import load_rows
from .rewards import RewardBreakdown, evaluate_cached
from .tasks import CATEGORY_WEIGHTS, SYSTEM_PROMPT, build_prompt, get_task


class TaskManagementTaskData(vf.TaskData):
    """Serializable data for one RLVR engineering task."""

    task_id: int
    params: dict[str, Any]
    framing: str = "spec"


class TaskManagementTask(vf.Task[TaskManagementTaskData]):
    """One engineering task; four executable reward functions share one test run."""

    async def _breakdown(self, trace: vf.Trace) -> RewardBreakdown:
        result = await evaluate_cached(self.data.task_id, self.data.params, trace.last_reply)
        trace.record_metric("test_reward", result.reward)
        return RewardBreakdown.from_result(result)

    @vf.reward(weight=CATEGORY_WEIGHTS["functional"])
    async def functional_reward(self, trace: vf.Trace) -> float:
        return (await self._breakdown(trace)).functional

    @vf.reward(weight=CATEGORY_WEIGHTS["database"])
    async def database_reward(self, trace: vf.Trace) -> float:
        return (await self._breakdown(trace)).database

    @vf.reward(weight=CATEGORY_WEIGHTS["security"])
    async def security_reward(self, trace: vf.Trace) -> float:
        return (await self._breakdown(trace)).security

    @vf.reward(weight=CATEGORY_WEIGHTS["quality"])
    async def quality_reward(self, trace: vf.Trace) -> float:
        return (await self._breakdown(trace)).quality


class TaskManagementTasksetConfig(vf.TasksetConfig):
    """Select split, task subset and sample limit."""

    split: str = "train"  # "train" | "validation"
    task_ids: list[int] | None = None  # e.g. [1, 2] for a smoke run; None = all six
    num_samples: int = -1
    random_seed: int = 42
    balance_tasks: bool = True  # train split only: equalise rows per task (see dataset.balance_by_task)


class TaskManagementTaskset(vf.Taskset[TaskManagementTask, TaskManagementTasksetConfig]):
    """Load the SDLC task rows as Verifiers v1 tasks."""

    def load(self) -> Iterator[TaskManagementTask]:
        rows = load_rows(self.config.split, self.config.task_ids, self.config.num_samples,
                         self.config.random_seed, self.config.balance_tasks)
        for row in rows:
            spec = get_task(row["task_id"])
            data = TaskManagementTaskData(
                id=row["id"],
                name=spec.title,
                prompt=build_prompt(spec, row["params"], row["framing"], row["with_examples"]),
                system_prompt=SYSTEM_PROMPT,
                task_id=row["task_id"],
                params=row["params"],
                framing=row["framing"],
            )
            yield TaskManagementTask(data, self.config.task)


# The v1 taskset loader discovers the exported Taskset class through __all__.
__all__ = ["TaskManagementTaskset"]


def main() -> None:
    """Load a few rows to verify the taskset locally (no database needed)."""
    taskset = TaskManagementTaskset(TaskManagementTasksetConfig(split="train", num_samples=6))
    tasks = list(taskset)
    print(f"Tasks loaded: {len(tasks)}")
    for t in tasks:
        print(f"  {t.data.id}: task {t.data.task_id}, prompt chars {len(t.data.prompt or '')}")
    if tasks:
        print(f"Registered rewards: {[fn.__name__ for fn in tasks[0].hooks('reward')]}")


if __name__ == "__main__":
    main()

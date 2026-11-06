"""Offline unit tests: artifact parsing, task registry, dataset, reward maths, cache.

No database, model or network needed (the environment test needs `verifiers`).
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from task_management_rlvr import evaluator, rewards, tasks
from task_management_rlvr.dataset import load_rows, split_rows
from task_management_rlvr.validators import parse_artifact, render_artifact

OWNED = frozenset({"app/security.py", "app/routers/auth.py"})
GOOD = '<implementation>\n<file path="app/security.py">\nX = 1\n</file>\n</implementation>'


# ------------------------------ validators --------------------------------
def test_parses_a_clean_artifact():
    art = parse_artifact(GOOD, OWNED)
    assert art.clean and art.files == {"app/security.py": "X = 1\n"}


def test_prose_around_block_is_tolerated_and_last_block_wins():
    text = "Sure!\n" + GOOD.replace("X = 1", "X = 0") + "\nrevised:\n" + GOOD
    assert parse_artifact(text, OWNED).files["app/security.py"] == "X = 1\n"


def test_markdown_fences_inside_file_are_stripped():
    text = GOOD.replace("X = 1", "```python\nX = 1\n```")
    assert parse_artifact(text, OWNED).files["app/security.py"] == "X = 1\n"


@pytest.mark.parametrize("path", ["../evil.py", "/etc/passwd", "app/../../x.py", "C:\\x.py", "app/other.py"])
def test_unsafe_or_unowned_paths_are_violations(path):
    text = f'<implementation>\n<file path="{path}">\nx=1\n</file>\n</implementation>'
    art = parse_artifact(text, OWNED)
    assert art.files == {} and art.violations and not art.clean


def test_duplicate_and_empty_files_are_violations():
    dup = GOOD.replace("</implementation>", '<file path="app/security.py">\nY = 2\n</file>\n</implementation>')
    assert parse_artifact(dup, OWNED).violations
    empty = '<implementation>\n<file path="app/security.py">\n\n</file>\n</implementation>'
    assert parse_artifact(empty, OWNED).violations


@pytest.mark.parametrize("reply", ["", "no block", "<implementation>unterminated", None, []])
def test_missing_block_yields_nothing(reply):
    art = parse_artifact(reply, OWNED)
    assert not art.usable and not art.found_block


def test_accepts_message_shapes():
    assert parse_artifact([{"role": "assistant", "content": GOOD}], OWNED).usable
    assert parse_artifact({"content": GOOD}, OWNED).usable


def test_render_round_trip():
    files = {"app/security.py": "A = 1\n", "app/routers/auth.py": "B = 2\n"}
    assert parse_artifact(render_artifact(files), OWNED).files == files


# --------------------------------- tasks -----------------------------------
def test_six_tasks_with_consistent_ownership():
    assert [t.task_id for t in tasks.TASKS] == [1, 2, 3, 4, 5, 6]
    ref = tasks.reference_dir()
    for t in tasks.TASKS:
        assert t.owned_files and not set(t.owned_files) & set(t.provided_files)
        for rel in (*t.owned_files, *t.provided_files):
            assert (ref / rel).is_file(), f"{t.slug}: {rel}"
        assert (tasks.project_root() / t.suite).is_file()


def test_weights_sum_to_one_and_match_design():
    assert tasks.CATEGORY_WEIGHTS == {"functional": 0.5, "database": 0.2, "security": 0.2, "quality": 0.1}
    assert sum(tasks.CATEGORY_WEIGHTS.values()) == pytest.approx(1.0)


def _markers(path: Path) -> dict[str, int]:
    counts = {c: 0 for c in tasks.CATEGORY_WEIGHTS} | {"hidden": 0}
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            for dec in node.decorator_list:
                name = ast.unparse(dec)
                for key in counts:
                    if name == f"pytest.mark.{key}":
                        counts[key] += 1
    return counts


@pytest.mark.parametrize("spec", tasks.TASKS, ids=lambda t: t.slug)
def test_every_suite_covers_all_categories_and_has_hidden_tests(spec):
    counts = _markers(tasks.project_root() / spec.suite)
    for cat in tasks.CATEGORY_WEIGHTS:
        assert counts[cat] > 0, f"{spec.slug} has no {cat} tests"
    assert counts["hidden"] > 0


def test_every_prompt_renders_and_mentions_owned_files():
    for row in tasks.all_rows():
        spec = tasks.get_task(row["task_id"])
        prompt = tasks.build_prompt(spec, row["params"], row["framing"], row["with_examples"])
        assert all(f in prompt for f in spec.owned_files)
        assert len(prompt) < 30_000


def test_prompt_embeds_row_parameters():
    spec = tasks.get_task(5)
    prompt = tasks.build_prompt(spec, {"password_min_length": 12, "title_max_length": 200})
    assert "200" in prompt and "12" in prompt


def test_stubs_are_extracted_without_bodies():
    stub = tasks.render_stub("app/services/project_service.py")
    assert "def delete_project(" in stub and "projects.lock" not in stub


# -------------------------------- dataset ---------------------------------
def test_rows_unique_and_split_is_disjoint_and_stratified():
    rows = tasks.all_rows()
    assert len({r["id"] for r in rows}) == len(rows) == 132
    train, held = split_rows(rows)
    assert not {r["id"] for r in train} & {r["id"] for r in held}
    for tid in range(1, 7):
        assert any(r["task_id"] == tid for r in train) and any(r["task_id"] == tid for r in held)


def test_train_split_is_balanced_across_tasks_but_validation_is_not_repeated():
    import collections

    train = load_rows("train")
    counts = collections.Counter(r["task_id"] for r in train)
    assert len(set(counts.values())) == 1 and set(counts) == set(range(1, 7))
    assert len({r["id"] for r in train}) == len(train)
    unbalanced = load_rows("train", balance=False)
    assert len(unbalanced) < len(train)
    val = load_rows("validation")
    assert len({r["id"] for r in val}) == len(val)


def test_load_rows_filters_limits_and_is_deterministic():
    assert {r["task_id"] for r in load_rows("train", [2, 3])} == {2, 3}
    a = load_rows("train", None, 7, seed=1)
    assert len(a) == 7 and a == load_rows("train", None, 7, seed=1)
    with pytest.raises(KeyError):
        load_rows("train", [99])
    with pytest.raises(ValueError):
        load_rows("bogus")


# --------------------------------- rewards --------------------------------
def test_breakdown_total_uses_design_weights():
    assert rewards.RewardBreakdown(1, 1, 1, 1).total == pytest.approx(1.0)
    assert rewards.RewardBreakdown(1, 0, 0, 0).total == pytest.approx(0.5)
    assert rewards.RewardBreakdown(0, 1, 1, 0).total == pytest.approx(0.4)
    assert rewards.RewardBreakdown().total == 0.0


def test_score_aggregation_counts_skips_and_errors_as_failures():
    rows = [{"nodeid": "f::a", "category": "functional", "passed": True},
            {"nodeid": "f::b", "category": "functional", "passed": False},
            {"nodeid": "f::c", "category": "security", "passed": True}]
    scores, counts, failed = evaluator._score(rows)
    assert scores["functional"] == 0.5 and scores["security"] == 1.0 and scores["database"] == 0.0
    assert counts["functional"] == (1, 2) and failed == ["b"]


def test_unusable_reply_scores_zero_without_touching_a_database():
    result = evaluator.evaluate_reply(1, {}, "I would use a CHECK constraint.")
    assert result.reward == 0.0 and result.error


def test_cache_key_is_stable_and_sensitive():
    k = rewards.cache_key(1, {"a": 1}, "x")
    assert k == rewards.cache_key(1, {"a": 1}, "x")
    assert len({k, rewards.cache_key(2, {"a": 1}, "x"), rewards.cache_key(1, {"a": 2}, "x"),
                rewards.cache_key(1, {"a": 1}, "y")}) == 4


def test_concurrent_reward_calls_share_one_evaluation(monkeypatch):
    calls = []

    def fake(task_id, params, reply):
        calls.append(task_id)
        return evaluator.EvalResult(scores={"functional": 1.0, "database": 0.5, "security": 0.0, "quality": 1.0})

    monkeypatch.setattr(rewards, "evaluate_reply", fake)
    rewards.clear_cache()

    async def go():
        return await asyncio.gather(*[rewards.evaluate_cached(3, {}, GOOD) for _ in range(4)])

    results = asyncio.run(go())
    assert calls == [3] and len({id(r) for r in results}) == 1


def test_infrastructure_errors_propagate_and_are_not_cached(monkeypatch):
    state = {"n": 0}

    def flaky(task_id, params, reply):
        state["n"] += 1
        if state["n"] == 1:
            raise evaluator.EvaluatorConfigError("db down")
        return evaluator.EvalResult()

    monkeypatch.setattr(rewards, "evaluate_reply", flaky)
    rewards.clear_cache()
    with pytest.raises(evaluator.EvaluatorConfigError):
        asyncio.run(rewards.evaluate_cached(1, {}, GOOD))
    asyncio.run(rewards.evaluate_cached(1, {}, GOOD))
    assert state["n"] == 2


# ------------------------------- environment -------------------------------
def test_taskset_loads_with_four_weighted_rewards():
    pytest.importorskip("verifiers.v1")
    from task_management_rlvr.environment import TaskManagementTaskset, TaskManagementTasksetConfig

    loaded = list(TaskManagementTaskset(TaskManagementTasksetConfig(split="validation", task_ids=[4])))
    assert loaded and all(t.data.task_id == 4 for t in loaded)
    fns = loaded[0].hooks("reward")
    assert sorted(f.__name__ for f in fns) == ["database_reward", "functional_reward", "quality_reward", "security_reward"]
    assert sum(getattr(f, "_vf_weight") for f in fns) == pytest.approx(1.0)

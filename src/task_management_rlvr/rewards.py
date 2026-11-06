"""Reward breakdown, weights and a de-duplicating async cache.

Verifiers calls each reward function separately, but all four categories come
from one test run.  The cache makes the run happen once per distinct
(task, params, reply) even when the four calls arrive concurrently.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from .evaluator import EvalResult, evaluate_reply
from .tasks import CATEGORY_WEIGHTS
from .validators import reply_text


@dataclass(frozen=True)
class RewardBreakdown:
    functional: float = 0.0
    database: float = 0.0
    security: float = 0.0
    quality: float = 0.0

    @classmethod
    def from_result(cls, result: EvalResult) -> "RewardBreakdown":
        return cls(**{c: float(result.scores[c]) for c in CATEGORY_WEIGHTS})

    @property
    def total(self) -> float:
        """Weighted total in [0, 1]: 0.5 functional + 0.2 database + 0.2 security + 0.1 quality."""
        return sum(CATEGORY_WEIGHTS[c] * getattr(self, c) for c in CATEGORY_WEIGHTS)


_LOCK = threading.Lock()
_CACHE: "OrderedDict[str, Future[EvalResult]]" = OrderedDict()
_MAX_ENTRIES = 2048


def cache_key(task_id: int, params: dict[str, Any], reply: str) -> str:
    blob = json.dumps([task_id, params, reply], sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _claim(key: str) -> "tuple[Future[EvalResult], bool]":
    """Return (future, is_owner). Exactly one caller per key is the owner and runs the work."""
    with _LOCK:
        future = _CACHE.get(key)
        if future is not None:
            _CACHE.move_to_end(key)
            return future, False
        future = Future()
        _CACHE[key] = future
        while len(_CACHE) > _MAX_ENTRIES:
            _CACHE.popitem(last=False)
        return future, True


async def evaluate_cached(task_id: int, params: dict[str, Any], completion: Any) -> EvalResult:
    """Evaluate once per (task, params, reply); concurrent callers share the result."""
    reply = reply_text(completion)
    key = cache_key(task_id, params, reply)
    future, owner = _claim(key)
    if owner:
        def run() -> None:
            try:
                future.set_result(evaluate_reply(task_id, params, reply))
            except BaseException as exc:  # infrastructure errors reach every waiter
                future.set_exception(exc)
                with _LOCK:
                    _CACHE.pop(key, None)  # never cache infrastructure failures

        await asyncio.get_running_loop().run_in_executor(None, run)
    return await asyncio.wrap_future(future)

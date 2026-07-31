"""The `job_type -> handler` registry.

Stage 4 shipped `EVALUATE`. Stage 5 added `IMPLEMENT` and `FIX_CODE` — A2,
Implementation_Plan §8 — sharing one handler module (`implement.py`) since
they differ only in which experiment they target and whether an LLM call is
involved, not in the render -> check -> commit path both end at. Stage 6 adds
`REVIEW` — A3, Implementation_Plan §9 — in its own module (`review.py`),
since its job is a verdict plus a research plan, not code. Every other
`job_type` in the schema's CHECK constraint (`aqrl/db/repositories/jobs.py`,
`JOB_TYPES`) is a real future stage, not a stub: `get_handler` raises
`NotImplementedHandler` for anything unregistered, which `failures.py`
classifies deterministic — one clear failure, not a retry storm, and a job
type nobody can service yet never silently succeeds.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from ...db.repositories.base import Row
from . import evaluate as _evaluate
from . import implement as _implement
from . import review as _review
from .base import HandlerResult, JobHandler, NotImplementedHandler

__all__ = ["HandlerResult", "JobHandler", "NotImplementedHandler", "get_handler"]


@dataclass(frozen=True)
class _FunctionHandler:
    run: Callable[[sqlite3.Connection, Row], Any]
    persist: Callable[[sqlite3.Connection, Row, Any], HandlerResult]


_HANDLERS: dict[str, _FunctionHandler] = {
    "EVALUATE": _FunctionHandler(run=_evaluate.run, persist=_evaluate.persist),
    "IMPLEMENT": _FunctionHandler(run=_implement.run, persist=_implement.persist),
    "FIX_CODE": _FunctionHandler(run=_implement.run, persist=_implement.persist),
    "REVIEW": _FunctionHandler(run=_review.run, persist=_review.persist),
}


def get_handler(job_type: str) -> JobHandler:
    try:
        return _HANDLERS[job_type]
    except KeyError:
        raise NotImplementedHandler(job_type) from None

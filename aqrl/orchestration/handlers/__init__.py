"""The `job_type -> handler` registry.

Stage 4 ships exactly one real handler — `EVALUATE` — because it is the only
job type with a producer built so far (Stage 3's engine). Every other
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
from .base import HandlerResult, JobHandler, NotImplementedHandler

__all__ = ["HandlerResult", "JobHandler", "NotImplementedHandler", "get_handler"]


@dataclass(frozen=True)
class _FunctionHandler:
    run: Callable[[sqlite3.Connection, Row], Any]
    persist: Callable[[sqlite3.Connection, Row, Any], HandlerResult]


_HANDLERS: dict[str, _FunctionHandler] = {
    "EVALUATE": _FunctionHandler(run=_evaluate.run, persist=_evaluate.persist),
}


def get_handler(job_type: str) -> JobHandler:
    try:
        return _HANDLERS[job_type]
    except KeyError:
        raise NotImplementedHandler(job_type) from None

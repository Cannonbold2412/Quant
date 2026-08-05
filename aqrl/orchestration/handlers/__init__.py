"""The `job_type -> handler` registry.

Stage 4 shipped `EVALUATE`. Stage 5 added `IMPLEMENT` and `FIX_CODE` — A2,
Implementation_Plan §8 — sharing one handler module (`implement.py`) since
they differ only in which experiment they target and whether an LLM call is
involved, not in the render -> check -> commit path both end at. Stage 6 adds
`REVIEW` — A3, Implementation_Plan §9 — in its own module (`review.py`),
since its job is a verdict plus a research plan, not code. Stage 8 adds
`PROMOTE` (A4, `promote.py`) and `ARCHIVE`/`MINE_PATTERNS` (A5,
`archive.py`, one module for both jobs the same way `implement.py` serves
two) — Implementation_Plan §11. Stage 11 adds `MONITOR_DEPLOYMENT`
(`monitor.py`) — Implementation_Plan §14. Every other `job_type` in the
schema's CHECK constraint (`aqrl/db/repositories/jobs.py`, `JOB_TYPES`) is a
real future stage, not a stub: `get_handler` raises `NotImplementedHandler`
for anything unregistered, which `failures.py` classifies deterministic —
one clear failure, not a retry storm, and a job type nobody can service yet
never silently succeeds.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from ...db.repositories.base import Row
from . import archive as _archive
from . import evaluate as _evaluate
from . import generate as _generate
from . import implement as _implement
from . import librarian as _librarian
from . import monitor as _monitor
from . import promote as _promote
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
    "GENERATE_SPEC": _FunctionHandler(run=_generate.run, persist=_generate.persist),
    "PROMOTE": _FunctionHandler(run=_promote.run, persist=_promote.persist),
    "ARCHIVE": _FunctionHandler(run=_archive.run_archive, persist=_archive.persist_archive),
    "MINE_PATTERNS": _FunctionHandler(run=_archive.run_mine, persist=_archive.persist_mine),
    "COLLECT_PAPERS": _FunctionHandler(run=_librarian.run_collect, persist=_librarian.persist_collect),
    "EXTRACT_KNOWLEDGE": _FunctionHandler(run=_librarian.run_extract, persist=_librarian.persist_extract),
    "COLLECT_MARKET_DATA": _FunctionHandler(
        run=_librarian.run_market_stats, persist=_librarian.persist_market_stats
    ),
    "MONITOR_DEPLOYMENT": _FunctionHandler(run=_monitor.run, persist=_monitor.persist),
}


def get_handler(job_type: str) -> JobHandler:
    try:
        return _HANDLERS[job_type]
    except KeyError:
        raise NotImplementedHandler(job_type) from None

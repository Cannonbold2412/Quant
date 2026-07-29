"""The handler contract every `job_type` implements.

Split into `run` (the actual work — may be slow, must not hold a database
write lock) and `persist` (a short, atomic write of the outcome plus any
follow-on event). `worker.py` is what enforces the ordering: it calls `run`
with no open transaction, then wraps `persist` in `BEGIN IMMEDIATE` so the
whole write — result rows, state transitions, the next job — commits or
rolls back as one unit. Splitting the two is what keeps a multi-minute
evaluation from holding SQLite's single write lock for its entire duration,
which would otherwise serialise every other worker and the scheduler's own
`claim()` behind it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from ...db.repositories.base import Row

__all__ = ["HandlerResult", "JobHandler", "NotImplementedHandler"]


class NotImplementedHandler(Exception):
    """No handler is registered for this `job_type`.

    Deliberately a plain, unclassified-as-transient exception: `failures.py`
    buckets it `deterministic` by default, so an unimplemented job type fails
    once with a clear message instead of retrying into a storm.
    """

    def __init__(self, job_type: str) -> None:
        self.job_type = job_type
        super().__init__(f"no handler registered for job_type {job_type!r}")


@dataclass(frozen=True)
class HandlerResult:
    tokens_spent: int = 0


class JobHandler(Protocol):
    def run(self, conn: sqlite3.Connection, job: Row) -> Any:
        """The work. No open transaction; safe to take a long time."""
        ...

    def persist(self, conn: sqlite3.Connection, job: Row, outcome: Any) -> HandlerResult:
        """Write the outcome and emit any follow-on event. Runs inside the
        caller's `BEGIN IMMEDIATE` transaction — keep it fast."""
        ...

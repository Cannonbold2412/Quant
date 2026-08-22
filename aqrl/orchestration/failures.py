"""Failure classification, retry backoff, and poison-pill quarantine (TRD §4.3).

TRD §4.3 draws one line that this module exists to enforce: **transient**
failures (API timeout, rate limit, a lock contention blip) retry with
backoff; **deterministic** ones (code that will never compile, a spec that
fails validation) do not — retrying them just burns budget reproducing the
same failure. `classify()` is the line; `handle_job_failure()` is what a
worker calls when a job fails, and it is also where poison-pill protection
lives: *"a strategy whose jobs fail k times consecutively is quarantined for
human inspection rather than looping forever"* (Implementation_Plan §6).
"""
from __future__ import annotations

import sqlite3

from ..config import get_settings
from ..db.repositories import JobRepository, Row, StrategyRepository, utcnow_iso
from .states import transition

__all__ = [
    "classify",
    "compute_backoff",
    "handle_job_failure",
    "maybe_quarantine",
]

#: Exception type names treated as transient regardless of message — infra
#: hiccups, not code or research problems.
_TRANSIENT_EXCEPTION_NAMES = frozenset(
    {
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "OperationalError",  # sqlite3: typically "database is locked"
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
    }
)

_TRANSIENT_MESSAGE_MARKERS = (
    "timeout",
    "timed out",
    "rate limit",
    "temporarily unavailable",
    "connection reset",
    "database is locked",
    "service unavailable",
    "429",
    "503",
)

_BASE_BACKOFF_SECONDS = 30
_MAX_BACKOFF_SECONDS = 3600


def classify(*, exc: BaseException | None = None, exit_code: int | None = None, message: str | None = None) -> str:
    """`"transient"` or `"deterministic"` — never anything else.

    A worker subprocess killed by a signal (negative return code on POSIX, or
    the conventional 137 for a container OOM-kill) is an infrastructure event,
    not the job's fault, so it classifies as transient even with no exception
    object to inspect.
    """
    if exit_code is not None and (exit_code < 0 or exit_code == 137):
        return "transient"

    if exc is not None:
        if type(exc).__name__ in _TRANSIENT_EXCEPTION_NAMES:
            return "transient"
        if isinstance(exc, sqlite3.OperationalError) and "lock" in str(exc).lower():
            return "transient"

    if message and any(marker in message.lower() for marker in _TRANSIENT_MESSAGE_MARKERS):
        return "transient"

    return "deterministic"


def compute_backoff(attempts: int, job_id: int = 0) -> int:
    """Exponential backoff, base 30s, capped at 1h, with per-job jitter.

    Jitter is deterministic (derived from `job_id`, not `random`) so retry
    timing is reproducible in tests while still spreading retries across jobs
    that failed in the same tick, avoiding a thundering herd on the next one.
    """
    backoff = min(_BASE_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0)), _MAX_BACKOFF_SECONDS)
    jitter = (job_id * 7 + attempts) % max(backoff // 4, 1)
    return backoff + jitter


def handle_job_failure(
    conn: sqlite3.Connection,
    job: Row,
    worker_id: str,
    *,
    error_message: str | None,
    error_trace: str | None = None,
    exc: BaseException | None = None,
    exit_code: int | None = None,
    now: str | None = None,
) -> str:
    """Decide retry vs. permanent failure, then check for quarantine.

    Returns `"retried"`, `"failed"`, or `"quarantined"`. Must run inside a
    transaction the caller controls (mirrors `states.transition` and
    `events.emit`) so the failure classification, the retry/fail write, and
    the quarantine check are one atomic step — a crash between them must not
    leave a job silently unclaimed with no visible reason.
    """
    now = now or utcnow_iso()
    jobs = JobRepository(conn)
    failure_class = classify(exc=exc, exit_code=exit_code, message=error_message)
    attempts = int(job["attempts"])
    max_attempts = int(job["max_attempts"])

    if failure_class == "transient" and attempts < max_attempts:
        backoff = compute_backoff(attempts, job["id"])
        scheduled_for = _add_seconds(now, backoff)
        jobs.requeue(
            job["id"],
            scheduled_for=scheduled_for,
            error_message=error_message,
            error_trace=error_trace,
            failure_class=failure_class,
            now=now,
        )
        result = "retried"
    else:
        jobs.fail(
            job["id"],
            worker_id,
            error_message=error_message,
            error_trace=error_trace,
            failure_class=failure_class,
            now=now,
        )
        result = "failed"

    strategy_id = job["strategy_id"]
    if strategy_id is not None and result == "failed":
        # The audit trail must say *why* a strategy is quarantined (states.py):
        # the streak length that tripped it, plus the failure that tipped it over.
        streak = get_settings().quarantine_after_failures
        reason = (
            f"{streak} consecutive job failures — latest: "
            f"job {job['uid']} ({job['job_type']}) failed: {error_message or failure_class}"
        )
        if maybe_quarantine(conn, strategy_id, reasoning=reason):
            result = "quarantined"

    return result


def maybe_quarantine(
    conn: sqlite3.Connection,
    strategy_id: int,
    *,
    threshold: int | None = None,
    reasoning: str | None = None,
) -> bool:
    """Quarantine `strategy_id` if its `threshold` most recent terminal jobs
    were all failures — poison-pill protection (Implementation_Plan §6).

    "Consecutive" is read strictly: the most recent `threshold` jobs to reach
    a terminal state must *all* be `failed`/`timed_out`, with no `succeeded`
    among them and no run of interleaved success resetting the count. A
    `cancelled` job (e.g. one drained by an earlier quarantine) does not count
    as either — it never ran to a verdict — so it is excluded from the window
    entirely rather than breaking or extending the streak.
    """
    threshold = threshold or get_settings().quarantine_after_failures
    strategies = StrategyRepository(conn)
    strategy = strategies.get(strategy_id)
    if strategy is None or strategy["quarantined"]:
        return False

    rows = conn.execute(
        """SELECT status FROM jobs WHERE strategy_id = ? AND status IN ('succeeded', 'failed', 'timed_out')
            ORDER BY id DESC LIMIT ?""",
        (strategy_id, threshold),
    ).fetchall()
    if len(rows) < threshold or any(row["status"] not in ("failed", "timed_out") for row in rows):
        return False

    reason = reasoning or f"{threshold} consecutive job failures"
    transition(
        conn,
        "strategies",
        strategy_id,
        "quarantined",
        actor="scheduler",
        reasoning=reason,
        quarantined=True,
        quarantine_reason=reason,
    )

    jobs = JobRepository(conn)
    for pending in jobs.find(strategy_id=strategy_id, status="pending"):
        jobs.cancel(pending["id"], reason="strategy quarantined")

    return True


def _add_seconds(iso_timestamp: str, seconds: int) -> str:
    from datetime import datetime, timedelta

    parsed = datetime.fromisoformat(iso_timestamp)
    return (parsed + timedelta(seconds=seconds)).isoformat()

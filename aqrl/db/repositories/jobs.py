"""The `jobs` queue — atomic claim, lease/heartbeat, completion (TRD §4.3).

**Atomicity is the whole point.** `claim()` runs inside `BEGIN IMMEDIATE`
(`aqrl.db.connection.transaction(..., immediate=True)`), which takes SQLite's
write lock before the first statement rather than on the first write. That
serialises every concurrent `claim()` call across every process touching this
database file — the SELECT-then-UPDATE inside it cannot race, because no other
connection can hold a write transaction at the same time. This is exactly the
property `idx_jobs_dispatch (status, priority, scheduled_for)` was indexed for
in migration 0007, and it is what the Postgres swap later replaces with
`FOR UPDATE SKIP LOCKED` (TRD §3.2) — same contract, different lock.

**Lease expiry is `kill -9` recovery.** A worker that dies mid-job leaves its
row `claimed` or `running` with a `lease_expires_at` in the past.
`expire_leases()` is the only thing that notices; it runs once per scheduler
tick (App-Flow §13). Nothing about recovery depends on the dead process ever
running again — the *database* is the source of truth, not the process table.
"""
from __future__ import annotations

from typing import Any

from .base import Repository, Row, utcnow_iso

__all__ = [
    "JOB_STATUSES",
    "JOB_TYPES",
    "JobRepository",
    "LeaseLost",
    "UnknownJobType",
]

JOB_TYPES: frozenset[str] = frozenset(
    {
        "GENERATE_SPEC",
        "IMPLEMENT",
        "FIX_CODE",
        "EVALUATE",
        "REVIEW",
        "PROMOTE",
        "ARCHIVE",
        "MINE_PATTERNS",
        "EXTRACT_KNOWLEDGE",
        "COLLECT_PAPERS",
        "COLLECT_GITHUB",
        "COLLECT_MARKET_DATA",
        "MONITOR_DEPLOYMENT",
        "NULL_WORLD_RUN",
        "GENERATE_REPORT",
    }
)

JOB_STATUSES: frozenset[str] = frozenset(
    {"pending", "claimed", "running", "succeeded", "failed", "timed_out", "cancelled"}
)

_OPEN_STATUSES = ("claimed", "running")


class UnknownJobType(ValueError):
    """`job_type` is not one of `JOB_TYPES` — caught in Python before the CHECK
    constraint would, so a bad enqueue fails at the call site, not the SQL."""


class LeaseLost(RuntimeError):
    """A worker tried to heartbeat or complete a job it no longer owns.

    This is the guard against the second half of Stage 4's done-when — "zero
    duplicated work." If a lease expired and another worker reclaimed the job,
    the original (perhaps merely slow, not dead) worker must not be allowed to
    keep writing as though it still holds the lease.
    """


def _dep_satisfied_clause() -> str:
    return (
        "(depends_on_job_id IS NULL OR EXISTS ("
        "SELECT 1 FROM jobs d WHERE d.id = jobs.depends_on_job_id AND d.status = 'succeeded'"
        "))"
    )


class JobRepository(Repository):
    table = "jobs"
    json_columns = frozenset({"payload"})

    # -- enqueue -----------------------------------------------------------

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        strategy_id: int | None = None,
        experiment_id: int | None = None,
        priority: int = 0,
        depends_on_job_id: int | None = None,
        scheduled_for: str | None = None,
        max_attempts: int = 3,
        dedupe_key: str | None = None,
    ) -> int:
        """Insert a `pending` job. Idempotent on `dedupe_key`.

        A caller that races another producer of the *same* job (two scheduler
        ticks noticing the same due time-driven job, an event handler retried
        after a crash before its transaction committed) gets back the
        existing row's id rather than a second job — `idx_jobs_dedupe`
        (migration 0008) is what makes that a fast lookup rather than a scan.
        """
        if job_type not in JOB_TYPES:
            raise UnknownJobType(f"unknown job_type {job_type!r}; expected one of {sorted(JOB_TYPES)}")

        if dedupe_key is not None:
            existing = self.conn.execute(
                "SELECT id FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if existing is not None:
                return int(existing["id"])

        return self.insert(
            job_type=job_type,
            payload=payload or {},
            strategy_id=strategy_id,
            experiment_id=experiment_id,
            status="pending",
            priority=priority,
            depends_on_job_id=depends_on_job_id,
            scheduled_for=scheduled_for,
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
        )

    # -- claim / heartbeat ---------------------------------------------------

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 300,
        job_types: list[str] | None = None,
        now: str | None = None,
    ) -> Row | None:
        """Atomically claim the highest-priority eligible pending job.

        Must be called inside an `immediate=True` transaction (the caller —
        `dispatch.py` — owns that boundary so claim-then-spawn is one unit).
        Returns `None` if nothing is eligible.
        """
        now = now or utcnow_iso()
        params: list[Any] = [now]
        sql = (
            "SELECT id FROM jobs WHERE status = 'pending' "
            "AND (scheduled_for IS NULL OR scheduled_for <= ?) "
            f"AND {_dep_satisfied_clause()}"
        )
        if job_types:
            placeholders = ", ".join("?" for _ in job_types)
            sql += f" AND job_type IN ({placeholders})"
            params.extend(job_types)
        sql += (
            " ORDER BY priority DESC, (scheduled_for IS NULL) DESC, scheduled_for ASC, id ASC LIMIT 1"
        )

        candidate = self.conn.execute(sql, params).fetchone()
        if candidate is None:
            return None

        job_id = int(candidate["id"])
        lease_expires_at = _add_seconds(now, lease_seconds)
        cursor = self.conn.execute(
            """UPDATE jobs
                  SET status = 'claimed', claimed_by = ?, lease_expires_at = ?,
                      heartbeat_at = ?, attempts = attempts + 1,
                      started_at = COALESCE(started_at, ?)
                WHERE id = ? AND status = 'pending'""",
            (worker_id, lease_expires_at, now, now, job_id),
        )
        if cursor.rowcount != 1:
            # Lost the race despite BEGIN IMMEDIATE serialising writers — only
            # possible if the caller reused a connection outside that
            # transaction. Treat as "nothing eligible" rather than raising.
            return None
        return self.get(job_id)

    def heartbeat(self, job_id: int, worker_id: str, *, lease_seconds: int = 300, now: str | None = None) -> None:
        now = now or utcnow_iso()
        cursor = self.conn.execute(
            """UPDATE jobs SET heartbeat_at = ?, lease_expires_at = ?
                WHERE id = ? AND claimed_by = ? AND status IN ('claimed', 'running')""",
            (now, _add_seconds(now, lease_seconds), job_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise LeaseLost(f"job {job_id} is no longer held by {worker_id!r}")

    def mark_running(self, job_id: int, worker_id: str, *, now: str | None = None) -> None:
        now = now or utcnow_iso()
        cursor = self.conn.execute(
            """UPDATE jobs SET status = 'running', heartbeat_at = ?
                WHERE id = ? AND claimed_by = ? AND status = 'claimed'""",
            (now, job_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise LeaseLost(f"job {job_id} is no longer held by {worker_id!r}")

    # -- completion -----------------------------------------------------------

    def succeed(
        self,
        job_id: int,
        worker_id: str,
        *,
        tokens_spent: int = 0,
        duration_seconds: int | None = None,
        now: str | None = None,
    ) -> None:
        self._own_and_close(
            job_id, worker_id, "succeeded", tokens_spent, duration_seconds, now=now
        )

    def fail(
        self,
        job_id: int,
        worker_id: str,
        *,
        error_message: str | None = None,
        error_trace: str | None = None,
        failure_class: str | None = None,
        tokens_spent: int = 0,
        duration_seconds: int | None = None,
        now: str | None = None,
    ) -> None:
        """Mark permanently `failed`. Retry is a separate decision — see
        `failures.py`, which calls `requeue()` instead when a failure is
        transient and attempts remain."""
        self._own_and_close(
            job_id,
            worker_id,
            "failed",
            tokens_spent,
            duration_seconds,
            error_message=error_message,
            error_trace=error_trace,
            failure_class=failure_class,
            now=now,
        )

    def time_out(
        self, job_id: int, worker_id: str, *, error_message: str = "wall-clock budget exceeded", now: str | None = None
    ) -> None:
        self._own_and_close(job_id, worker_id, "timed_out", 0, None, error_message=error_message, now=now)

    def cancel(self, job_id: int, *, reason: str | None = None, now: str | None = None) -> None:
        """Cancel a job regardless of who (if anyone) holds it — used by
        quarantine (`failures.py`) to drain a strategy's pending queue."""
        now = now or utcnow_iso()
        self.conn.execute(
            """UPDATE jobs SET status = 'cancelled', error_message = COALESCE(?, error_message),
                      completed_at = ?
                WHERE id = ? AND status IN ('pending', 'claimed', 'running')""",
            (reason, now, job_id),
        )

    def requeue(
        self,
        job_id: int,
        *,
        scheduled_for: str | None = None,
        error_message: str | None = None,
        error_trace: str | None = None,
        failure_class: str | None = None,
        now: str | None = None,
    ) -> None:
        """Return a claimed/running job to `pending` for a transient retry.

        `attempts` is left as-is — it was already incremented at claim time,
        so `failures.py` can compare it against `max_attempts` without a
        second counter. The error fields are stored even though the job is
        `pending` again, so `aqrl jobs show` explains *why* a retried job was
        retried instead of looking untouched.
        """
        now = now or utcnow_iso()
        self.conn.execute(
            """UPDATE jobs
                  SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL,
                      heartbeat_at = NULL, scheduled_for = ?,
                      error_message = ?, error_trace = ?, failure_class = ?
                WHERE id = ? AND status IN ('claimed', 'running')""",
            (scheduled_for, error_message, error_trace, failure_class, job_id),
        )

    def _own_and_close(
        self,
        job_id: int,
        worker_id: str,
        status: str,
        tokens_spent: int,
        duration_seconds: int | None,
        *,
        error_message: str | None = None,
        error_trace: str | None = None,
        failure_class: str | None = None,
        now: str | None = None,
    ) -> None:
        now = now or utcnow_iso()
        cursor = self.conn.execute(
            """UPDATE jobs
                  SET status = ?, tokens_spent = tokens_spent + ?, duration_seconds = ?,
                      error_message = ?, error_trace = ?, failure_class = ?, completed_at = ?
                WHERE id = ? AND claimed_by = ? AND status IN ('claimed', 'running')""",
            (status, tokens_spent, duration_seconds, error_message, error_trace, failure_class, now, job_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise LeaseLost(f"job {job_id} is no longer held by {worker_id!r}")

    # -- lease recovery ---------------------------------------------------------

    def expire_leases(self, *, now: str | None = None) -> list[int]:
        """`kill -9` recovery: reclaim jobs whose lease ran out.

        A job under its `max_attempts` goes back to `pending` (the attempt
        already counted at claim time). One at its limit goes to `failed`
        instead — an unbounded reclaim/expire cycle is not "no duplicated
        work," it is duplicated work forever. Returns the ids touched, for
        the scheduler to log.
        """
        now = now or utcnow_iso()
        expired = self.conn.execute(
            f"SELECT id, attempts, max_attempts FROM jobs "
            f"WHERE status IN ({', '.join('?' for _ in _OPEN_STATUSES)}) AND lease_expires_at < ?",
            (*_OPEN_STATUSES, now),
        ).fetchall()

        touched: list[int] = []
        for row in expired:
            job_id = int(row["id"])
            if int(row["attempts"]) >= int(row["max_attempts"]):
                self.conn.execute(
                    """UPDATE jobs SET status = 'failed', claimed_by = NULL, lease_expires_at = NULL,
                              error_message = 'lease expired at max_attempts', completed_at = ?
                        WHERE id = ?""",
                    (now, job_id),
                )
            else:
                self.conn.execute(
                    """UPDATE jobs SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL,
                              heartbeat_at = NULL
                        WHERE id = ?""",
                    (job_id,),
                )
            touched.append(job_id)
        return touched

    # -- queries ---------------------------------------------------------------

    def pending_count(self, job_type: str | None = None) -> int:
        return self.count(**({"status": "pending"} if job_type is None else {"status": "pending", "job_type": job_type}))

    def running_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('claimed', 'running')"
        ).fetchone()["n"]

    def by_strategy(self, strategy_id: int, *, limit: int | None = None) -> list[Row]:
        return self.find(strategy_id=strategy_id, order_by="id DESC", limit=limit)

    def recent_failures(self, strategy_id: int, *, limit: int = 20) -> list[Row]:
        rows = self.conn.execute(
            """SELECT * FROM jobs WHERE strategy_id = ? AND status IN ('failed', 'timed_out')
                ORDER BY completed_at DESC LIMIT ?""",
            (strategy_id, limit),
        ).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[misc]

    def due_time_driven(self, dedupe_prefix: str) -> Row | None:
        """Most recent job whose `dedupe_key` starts with `dedupe_prefix`.

        Time-driven jobs (TRD §4.2) dedupe on a period-stamped key
        (`"nightly_a1_batch:2026-07-29"`); the scheduler uses this to decide
        whether today's/this-hour's instance has already fired.
        """
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE dedupe_key LIKE ? ORDER BY id DESC LIMIT 1",
            (f"{dedupe_prefix}%",),
        ).fetchone()
        return self._decode(row)


def _add_seconds(iso_timestamp: str, seconds: int) -> str:
    from datetime import datetime, timedelta

    parsed = datetime.fromisoformat(iso_timestamp)
    return (parsed + timedelta(seconds=seconds)).isoformat()

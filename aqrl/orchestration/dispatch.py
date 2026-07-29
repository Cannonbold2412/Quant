"""Spawn and reap worker subprocesses; enforce the per-experiment time budget.

Each job is one `python -m aqrl.orchestration.worker` subprocess (TRD §9.3,
§9.6). The `Dispatcher` tracks the ones *it* spawned in memory purely as an
optimisation — noticing a dead child immediately rather than waiting out its
lease. That optimisation is not where correctness lives: if the scheduler
process itself is killed, `_running` disappears with it, and recovery still
happens, just on the next scheduler's `expire_leases()` tick instead of
`reap()`. App-Flow §13 is explicit about this: *"killing the scheduler
mid-flight loses nothing... state lives entirely in the database."*
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import get_settings
from ..db import transaction
from ..db.repositories import JobRepository, LeaseLost
from .budgets import check_global
from .failures import handle_job_failure

__all__ = ["Dispatcher", "RunningJob"]


@dataclass
class RunningJob:
    job_id: int
    job_uid: str
    strategy_id: int | None
    popen: subprocess.Popen
    deadline: datetime
    terminated_at: datetime | None = None


class Dispatcher:
    """Owns the worker subprocesses one scheduler process spawned."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        worker_id: str,
        max_concurrent: int | None = None,
        time_budget_seconds: int = 1800,
        python_executable: str | None = None,
    ) -> None:
        self.conn = conn
        self.worker_id = worker_id
        settings = get_settings()
        self.max_concurrent = max_concurrent if max_concurrent is not None else settings.max_concurrent_workers
        self.lease_seconds = settings.default_lease_seconds
        self.time_budget_seconds = time_budget_seconds
        self.python_executable = python_executable or sys.executable
        self._running: dict[int, RunningJob] = {}

    @property
    def running_count(self) -> int:
        return len(self._running)

    # -- dispatch ---------------------------------------------------------------

    def dispatch_pending(self, *, job_types: list[str] | None = None) -> list[int]:
        """Claim and spawn jobs until the concurrency cap or a budget stops it.

        Returns the ids dispatched. An empty list with `running_count` already
        at `max_concurrent` means "concurrency cap" as the idle cause; an
        empty list with a global budget exhausted means "budget_exhausted" —
        `scheduler.py` is what turns this into the TRD §4.5 idle-cause report.
        """
        dispatched: list[int] = []
        jobs = JobRepository(self.conn)
        while self.running_count < self.max_concurrent:
            back_pressure = check_global(self.conn)
            if not back_pressure.allowed:
                break
            with transaction(self.conn, immediate=True):
                job = jobs.claim(self.worker_id, lease_seconds=self.lease_seconds, job_types=job_types)
            if job is None:
                break
            self._spawn(job)
            dispatched.append(job["id"])
        return dispatched

    def _spawn(self, job) -> None:
        # `--worker-id` must match what `claim()` just wrote as `claimed_by` —
        # otherwise the worker's own `mark_running`/`succeed`/`fail` calls
        # fail their ownership check against a lease they never held under
        # their own name, and every dispatched job dies instantly with
        # `LeaseLost` before doing any work.
        popen = subprocess.Popen(
            [
                self.python_executable, "-m", "aqrl.orchestration.worker",
                "--job-uid", job["uid"],
                "--worker-id", self.worker_id,
                "--lease-seconds", str(self.lease_seconds),
            ]
        )
        deadline = datetime.now(UTC) + timedelta(seconds=self.time_budget_seconds)
        self._running[job["id"]] = RunningJob(job["id"], job["uid"], job["strategy_id"], popen, deadline)

    # -- time budget --------------------------------------------------------------

    def enforce_time_budgets(self, *, grace_seconds: int = 10) -> list[int]:
        """SIGTERM anything overrun; SIGKILL anything that ignored SIGTERM for
        `grace_seconds`. Returns job ids signalled this call."""
        now = datetime.now(UTC)
        signalled: list[int] = []
        for running in self._running.values():
            if running.popen.poll() is not None:
                continue
            if running.terminated_at is not None:
                if now >= running.terminated_at + timedelta(seconds=grace_seconds):
                    running.popen.kill()
                    signalled.append(running.job_id)
                continue
            if now >= running.deadline:
                running.popen.terminate()
                running.terminated_at = now
                signalled.append(running.job_id)
        return signalled

    # -- reap -----------------------------------------------------------------

    def reap(self) -> list[int]:
        """Collect subprocesses that have exited.

        If the worker already closed the job out (`succeeded`/`failed`/
        `pending` via retry) there is nothing to do. If the job is still
        `claimed`/`running`, the process died without recording anything —
        `SIGKILL`, OOM, a segfault — and this is treated as a job failure
        classified from the exit code, same as any other failure path.
        """
        finished: list[int] = []
        jobs = JobRepository(self.conn)
        for job_id in list(self._running):
            running = self._running[job_id]
            code = running.popen.poll()
            if code is None:
                continue
            finished.append(job_id)
            del self._running[job_id]

            was_overrun = running.terminated_at is not None
            try:
                with transaction(self.conn, immediate=True):
                    current = jobs.get(job_id)
                    if current is None or current["status"] not in ("claimed", "running"):
                        continue
                    message = (
                        f"worker exceeded its {self.time_budget_seconds}s time budget"
                        if was_overrun
                        else f"worker exited with code {code} without recording a result"
                    )
                    handle_job_failure(
                        self.conn,
                        current,
                        current["claimed_by"],
                        error_message=message,
                        exit_code=code,
                    )
            except LeaseLost:
                pass  # reclaimed by lease expiry before we got here; nothing to do
        return finished

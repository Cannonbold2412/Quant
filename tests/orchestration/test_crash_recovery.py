"""Stage 4's actual done-when (Implementation_Plan §6):

    "the scheduler survives `kill -9` mid-job with zero state loss and zero
    duplicated work."

Two shapes of that guarantee, tested against real OS processes rather than
mocks: killing the *worker* mid-job, and losing the *scheduler* itself
(its in-memory bookkeeping) while a job is outstanding.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

from aqrl.db import transaction
from aqrl.db.repositories import EvaluationRepository, JobRepository
from aqrl.orchestration.dispatch import Dispatcher
from aqrl.orchestration.worker import run_job


def _claim(conn, jobs: JobRepository, worker_id: str, lease_seconds: int):
    with transaction(conn, immediate=True):
        return jobs.claim(worker_id, lease_seconds=lease_seconds)


def test_kill_9_loses_nothing(conn, strategy_id, experiment_id, evaluate_job_id, loose_bar):
    """SIGKILL a real worker subprocess mid-job. No partial write survives,
    and a subsequent re-run produces exactly one evaluation — no duplicate,
    no loss."""
    jobs = JobRepository(conn)
    worker_id = "crash-test-worker"
    claimed = _claim(conn, jobs, worker_id, lease_seconds=10)
    assert claimed["id"] == evaluate_job_id

    process = subprocess.Popen(
        [
            sys.executable, "-m", "aqrl.orchestration.worker",
            "--job-uid", claimed["uid"],
            "--worker-id", worker_id,
            "--lease-seconds", "10",
        ],
    )
    time.sleep(0.15)  # let it past interpreter startup, into the handler
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=15)

    # Nothing durable happened: either the job never even reached `running`,
    # or it did and the transaction that would have persisted the outcome
    # never committed (SQLite only makes a transaction's writes visible at
    # COMMIT — a killed process has committed nothing since its last commit).
    row = jobs.get(evaluate_job_id)
    assert row["status"] in ("claimed", "running"), row
    assert EvaluationRepository(conn).latest_for_experiment(experiment_id) is None

    # Recovery: the lease expires, the orphaned job returns to `pending`.
    lease_expires_at = row["lease_expires_at"]
    past_expiry = (datetime.fromisoformat(lease_expires_at) + timedelta(seconds=1)).isoformat()
    touched = jobs.expire_leases(now=past_expiry)
    assert evaluate_job_id in touched
    assert jobs.get(evaluate_job_id)["status"] == "pending"

    # Re-run for real, to completion — no half-finished state to trip over.
    reclaimed = _claim(conn, jobs, "crash-test-worker-2", lease_seconds=60)
    assert reclaimed["id"] == evaluate_job_id
    code = run_job(reclaimed["uid"], worker_id="crash-test-worker-2")
    assert code == 0

    final = jobs.get(evaluate_job_id)
    assert final["status"] == "succeeded"
    evaluations = EvaluationRepository(conn).for_experiment(experiment_id)
    assert len(evaluations) == 1, "exactly one evaluation — the killed attempt left nothing behind"


def test_scheduler_kill_9_orphans_recover(conn, strategy_id):
    """Losing the *scheduler* process (not just a worker) loses only its
    in-memory `_running` bookkeeping — the next scheduler's `tick()` still
    reclaims the job via lease expiry, per App-Flow §13's restart-safety
    claim. No real subprocess needed here: what's under test is
    `expire_leases` plus a fresh `Dispatcher`'s next tick, not process
    signals (`test_kill_9_loses_nothing` already covers the signal path)."""
    jobs = JobRepository(conn)
    job_id = jobs.enqueue("FIX_CODE", strategy_id=strategy_id)

    # A job claimed by a worker that then vanished along with its scheduler —
    # no heartbeat, no completion, ever.
    claimed = _claim(conn, jobs, "worker-that-died-with-its-scheduler", lease_seconds=1)
    assert claimed["id"] == job_id

    # "The next scheduler": brand-new Dispatcher, no memory of the above.
    fresh_dispatcher = Dispatcher(conn, worker_id="scheduler-B", max_concurrent=2)
    assert fresh_dispatcher.running_count == 0

    future = datetime.fromisoformat(claimed["lease_expires_at"]) + timedelta(seconds=1)
    from aqrl.orchestration.scheduler import tick

    report = tick(conn, fresh_dispatcher, now=future)
    assert job_id in report.expired_leases
    assert job_id in report.dispatched, "reclaimed and re-dispatched in the same tick that expired it"

    for running in fresh_dispatcher._running.values():
        running.popen.wait(timeout=15)
    fresh_dispatcher.reap()

    final = jobs.get(job_id)
    assert final["status"] in ("failed", "pending")  # FIX_CODE has no handler -> deterministic failure
    assert final["attempts"] == 2, "claimed exactly twice: the orphaned attempt, then the recovery"

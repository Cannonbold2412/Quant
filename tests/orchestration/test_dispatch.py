"""`Dispatcher` — spawning, reaping, concurrency and time-budget enforcement.

Uses real `COLLECT_PAPERS` jobs (a valid `job_type` with no registered
handler — still true post-Stage 8; that's Stage 10's collector work) to
exercise spawn/reap against genuine subprocesses without paying for a real
evaluation run — the worker fails fast with `NotImplementedHandler`, which is
exactly the "job type nobody can service" path `handlers/__init__.py`
documents. `enforce_time_budgets` is tested against a fake `Popen` double
instead, since a real overrun would mean a real multi-minute sleep.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aqrl.db.repositories import JobRepository, StrategyRepository
from aqrl.orchestration.budgets import BudgetRepository
from aqrl.orchestration.dispatch import Dispatcher, RunningJob


@pytest.fixture
def strategy_id(conn) -> int:
    return StrategyRepository(conn).insert(name="n", family="f", market="m", timeframe="t")


def test_dispatch_pending_respects_concurrency_cap(conn, strategy_id):
    jobs = JobRepository(conn)
    for _ in range(5):
        jobs.enqueue("COLLECT_PAPERS", strategy_id=strategy_id)

    dispatcher = Dispatcher(conn, worker_id="test-scheduler", max_concurrent=2)
    dispatched = dispatcher.dispatch_pending()

    assert len(dispatched) == 2
    assert dispatcher.running_count == 2
    assert jobs.pending_count() == 3

    for running in dispatcher._running.values():
        running.popen.wait(timeout=15)
    dispatcher.reap()


def test_reap_records_notimplementedhandler_as_a_deterministic_failure(conn, strategy_id):
    jobs = JobRepository(conn)
    job_id = jobs.enqueue("COLLECT_PAPERS", strategy_id=strategy_id)

    dispatcher = Dispatcher(conn, worker_id="test-scheduler", max_concurrent=1)
    dispatched = dispatcher.dispatch_pending()
    assert dispatched == [job_id]

    for running in dispatcher._running.values():
        running.popen.wait(timeout=15)
    reaped = dispatcher.reap()
    assert reaped == [job_id]

    row = jobs.get(job_id)
    assert row["status"] == "failed"
    assert row["failure_class"] == "deterministic"
    assert "no handler registered" in row["error_message"]


def test_dispatch_pending_stops_when_the_global_budget_is_exhausted(conn, strategy_id):
    jobs = JobRepository(conn)
    jobs.enqueue("COLLECT_PAPERS", strategy_id=strategy_id)
    BudgetRepository(conn).upsert("global", "experiments", "day", 0)

    dispatcher = Dispatcher(conn, worker_id="test-scheduler", max_concurrent=4)
    dispatched = dispatcher.dispatch_pending()

    assert dispatched == []
    assert dispatcher.running_count == 0
    assert jobs.pending_count() == 1


# -- enforce_time_budgets: a fake Popen, no real sleeping ------------------------


class _FakePopen:
    """Simulates an unresponsive process: `terminate()` alone never ends it —
    only `kill()` does — so the grace-period escalation path is exercised."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_enforce_time_budgets_terminates_then_kills_after_grace():
    dispatcher = Dispatcher.__new__(Dispatcher)  # bypass __init__ — no real conn needed
    dispatcher._running = {}
    popen = _FakePopen()
    now = datetime.now(UTC)
    dispatcher._running[1] = RunningJob(
        job_id=1, job_uid="u1", strategy_id=None, popen=popen, deadline=now - timedelta(seconds=1)
    )

    signalled = dispatcher.enforce_time_budgets(grace_seconds=0)
    assert signalled == [1]
    assert popen.terminated

    # a second pass, past the (zero-length) grace period, escalates to kill
    signalled_again = dispatcher.enforce_time_budgets(grace_seconds=0)
    assert signalled_again == [1]
    assert popen.killed


def test_enforce_time_budgets_leaves_jobs_within_budget_alone():
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher._running = {}
    popen = _FakePopen()
    dispatcher._running[1] = RunningJob(
        job_id=1, job_uid="u1", strategy_id=None, popen=popen, deadline=datetime.now(UTC) + timedelta(hours=1)
    )

    assert dispatcher.enforce_time_budgets() == []
    assert not popen.terminated

"""Classification, backoff, and poison-pill quarantine (TRD §4.3)."""
from __future__ import annotations

import sqlite3

import pytest

from aqrl.db import transaction
from aqrl.db.repositories import JobRepository, StrategyRepository
from aqrl.orchestration.failures import classify, compute_backoff, handle_job_failure, maybe_quarantine


# -- classify -----------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"exc": TimeoutError("slow")}, "transient"),
        ({"exc": ConnectionResetError("reset")}, "transient"),
        ({"exc": ValueError("bad spec")}, "deterministic"),
        ({"exit_code": -9}, "transient"),  # SIGKILL
        ({"exit_code": 137}, "transient"),  # container OOM-kill convention
        ({"exit_code": 1}, "deterministic"),
        ({"message": "429 Too Many Requests"}, "transient"),
        ({"message": "spec failed validation"}, "deterministic"),
        ({}, "deterministic"),
    ],
)
def test_classify(kwargs, expected):
    assert classify(**kwargs) == expected


def test_classify_sqlite_lock_error_is_transient():
    err = sqlite3.OperationalError("database is locked")
    assert classify(exc=err) == "transient"


# -- compute_backoff ------------------------------------------------------------


def test_backoff_grows_and_caps():
    values = [compute_backoff(attempt, job_id=1) for attempt in range(1, 10)]
    # each attempt's floor (ignoring jitter) roughly doubles, capped at 1h
    assert values[0] < values[1] < values[2]
    assert all(v <= 3600 + 3600 // 4 for v in values)


def test_backoff_is_deterministic_per_job():
    assert compute_backoff(3, job_id=42) == compute_backoff(3, job_id=42)


# -- handle_job_failure ---------------------------------------------------------


@pytest.fixture
def strategy_id(conn) -> int:
    return StrategyRepository(conn).insert(name="n", family="f", market="m", timeframe="t")


def _claim(conn, jobs: JobRepository, worker: str = "w1"):
    with transaction(conn, immediate=True):
        return jobs.claim(worker, lease_seconds=300)


def test_transient_failure_is_retried_when_attempts_remain(conn, strategy_id):
    jobs = JobRepository(conn)
    jobs.enqueue("EVALUATE", strategy_id=strategy_id, max_attempts=3)
    claimed = _claim(conn, jobs)

    with transaction(conn, immediate=True):
        current = jobs.get(claimed["id"])
        result = handle_job_failure(conn, current, "w1", error_message="rate limit hit", exc=TimeoutError())

    assert result == "retried"
    row = jobs.get(claimed["id"])
    assert row["status"] == "pending"
    assert row["scheduled_for"] is not None


def test_deterministic_failure_is_never_retried(conn, strategy_id):
    jobs = JobRepository(conn)
    jobs.enqueue("EVALUATE", strategy_id=strategy_id, max_attempts=5)
    claimed = _claim(conn, jobs)

    with transaction(conn, immediate=True):
        current = jobs.get(claimed["id"])
        result = handle_job_failure(conn, current, "w1", error_message="spec invalid", exc=ValueError("bad"))

    assert result == "failed"
    assert jobs.get(claimed["id"])["status"] == "failed"


def test_transient_failure_fails_permanently_once_attempts_exhausted(conn, strategy_id):
    jobs = JobRepository(conn)
    jobs.enqueue("EVALUATE", strategy_id=strategy_id, max_attempts=1)
    claimed = _claim(conn, jobs)

    with transaction(conn, immediate=True):
        current = jobs.get(claimed["id"])
        result = handle_job_failure(conn, current, "w1", error_message="timeout", exc=TimeoutError())

    assert result == "failed"


# -- maybe_quarantine -----------------------------------------------------------


def test_quarantine_after_k_consecutive_failures(conn, strategy_id):
    jobs = JobRepository(conn)
    for _ in range(3):
        jobs.enqueue("EVALUATE", strategy_id=strategy_id, max_attempts=1)
        claimed = _claim(conn, jobs)
        with transaction(conn, immediate=True):
            current = jobs.get(claimed["id"])
            handle_job_failure(conn, current, "w1", error_message="boom", exc=ValueError("x"))

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["quarantined"] == 1
    assert strategy["status"] == "quarantined"
    assert "3" in strategy["quarantine_reason"] or "consecutive" in strategy["quarantine_reason"]


def test_quarantine_cancels_pending_jobs_for_the_strategy(conn, strategy_id):
    jobs = JobRepository(conn)
    survivor = jobs.enqueue("EVALUATE", strategy_id=strategy_id, max_attempts=1, priority=-1)
    for _ in range(3):
        jobs.enqueue("EVALUATE", strategy_id=strategy_id, max_attempts=1, priority=1)
        claimed = _claim(conn, jobs)
        with transaction(conn, immediate=True):
            current = jobs.get(claimed["id"])
            handle_job_failure(conn, current, "w1", error_message="boom", exc=ValueError("x"))

    assert jobs.get(survivor)["status"] == "cancelled"


def test_a_success_between_failures_resets_the_streak(conn, strategy_id):
    jobs = JobRepository(conn)
    for _ in range(2):
        jobs.enqueue("EVALUATE", strategy_id=strategy_id, max_attempts=1)
        claimed = _claim(conn, jobs)
        with transaction(conn, immediate=True):
            handle_job_failure(conn, jobs.get(claimed["id"]), "w1", error_message="boom", exc=ValueError("x"))

    jobs.enqueue("EVALUATE", strategy_id=strategy_id)
    claimed = _claim(conn, jobs)
    jobs.succeed(claimed["id"], "w1")

    for _ in range(2):
        jobs.enqueue("EVALUATE", strategy_id=strategy_id, max_attempts=1)
        claimed = _claim(conn, jobs)
        with transaction(conn, immediate=True):
            handle_job_failure(conn, jobs.get(claimed["id"]), "w1", error_message="boom", exc=ValueError("x"))

    assert StrategyRepository(conn).get(strategy_id)["quarantined"] == 0


def test_maybe_quarantine_is_a_noop_on_an_already_quarantined_strategy(conn, strategy_id):
    StrategyRepository(conn).update(strategy_id, status="quarantined", quarantined=True, quarantine_reason="x")
    assert maybe_quarantine(conn, strategy_id, threshold=1) is False

"""`JobRepository` — atomic claim, lease/heartbeat, dedupe (TRD §4.3)."""
from __future__ import annotations

import concurrent.futures
import threading

import pytest

from aqrl.db import connect, migrate, transaction
from aqrl.db.repositories import JobRepository, LeaseLost, UnknownJobType


@pytest.fixture
def jobs(conn) -> JobRepository:
    return JobRepository(conn)


def test_enqueue_rejects_unknown_job_type(jobs):
    with pytest.raises(UnknownJobType):
        jobs.enqueue("NOT_A_REAL_TYPE")


def test_enqueue_is_idempotent_on_dedupe_key(jobs):
    first = jobs.enqueue("EVALUATE", dedupe_key="k1")
    second = jobs.enqueue("EVALUATE", dedupe_key="k1")
    assert first == second
    assert jobs.count() == 1


def test_enqueue_without_dedupe_key_never_collides(jobs):
    first = jobs.enqueue("EVALUATE")
    second = jobs.enqueue("EVALUATE")
    assert first != second
    assert jobs.count() == 2


def test_claim_is_atomic_and_returns_highest_priority_first(conn, jobs):
    low = jobs.enqueue("EVALUATE", priority=0)
    high = jobs.enqueue("EVALUATE", priority=5)

    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=60)
    assert claimed["id"] == high

    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=60)
    assert claimed["id"] == low

    with transaction(conn, immediate=True):
        assert jobs.claim("w1", lease_seconds=60) is None


def test_claim_respects_job_types_filter(conn, jobs):
    jobs.enqueue("FIX_CODE")
    evaluate_id = jobs.enqueue("EVALUATE")
    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=60, job_types=["EVALUATE"])
    assert claimed["id"] == evaluate_id


def test_claim_skips_jobs_scheduled_in_the_future(conn, jobs):
    jobs.enqueue("EVALUATE", scheduled_for="2999-01-01T00:00:00+00:00")
    with transaction(conn, immediate=True):
        assert jobs.claim("w1", lease_seconds=60) is None


def test_claim_respects_unmet_dependency(conn, jobs):
    parent = jobs.enqueue("EVALUATE")
    jobs.enqueue("PROMOTE", depends_on_job_id=parent)
    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=60, job_types=["PROMOTE"])
    assert claimed is None

    with transaction(conn, immediate=True):
        parent_claim = jobs.claim("w1", lease_seconds=60, job_types=["EVALUATE"])
    jobs.succeed(parent_claim["id"], "w1")

    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=60, job_types=["PROMOTE"])
    assert claimed["id"] is not None


def test_heartbeat_extends_lease_and_rejects_the_wrong_worker(conn, jobs):
    jobs.enqueue("EVALUATE")
    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=60)
    first_lease = claimed["lease_expires_at"]

    jobs.heartbeat(claimed["id"], "w1", lease_seconds=120)
    assert jobs.get(claimed["id"])["lease_expires_at"] > first_lease

    with pytest.raises(LeaseLost):
        jobs.heartbeat(claimed["id"], "an-impostor")


def test_succeed_and_fail_require_ownership(conn, jobs):
    job_id = jobs.enqueue("EVALUATE")
    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=60)

    with pytest.raises(LeaseLost):
        jobs.succeed(claimed["id"], "an-impostor")

    jobs.succeed(claimed["id"], "w1", tokens_spent=7, duration_seconds=3)
    row = jobs.get(job_id)
    assert row["status"] == "succeeded"
    assert row["tokens_spent"] == 7
    assert row["duration_seconds"] == 3


def test_expire_leases_returns_orphans_to_pending(conn, jobs):
    job_id = jobs.enqueue("EVALUATE")
    with transaction(conn, immediate=True):
        jobs.claim("w1", lease_seconds=1)

    touched = jobs.expire_leases(now="2999-01-01T00:00:00+00:00")
    assert job_id in touched
    row = jobs.get(job_id)
    assert row["status"] == "pending"
    assert row["claimed_by"] is None
    assert row["lease_expires_at"] is None


def test_expire_leases_fails_permanently_at_max_attempts(conn, jobs):
    job_id = jobs.enqueue("EVALUATE", max_attempts=1)
    with transaction(conn, immediate=True):
        jobs.claim("w1", lease_seconds=1)

    touched = jobs.expire_leases(now="2999-01-01T00:00:00+00:00")
    assert job_id in touched
    assert jobs.get(job_id)["status"] == "failed"


def test_requeue_preserves_attempts_and_records_the_reason(conn, jobs):
    job_id = jobs.enqueue("EVALUATE")
    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=60)
    assert claimed["attempts"] == 1

    jobs.requeue(claimed["id"], error_message="rate limited", failure_class="transient")
    row = jobs.get(job_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["error_message"] == "rate limited"
    assert row["claimed_by"] is None


def test_cancel_drains_a_pending_job(conn, jobs):
    job_id = jobs.enqueue("EVALUATE")
    jobs.cancel(job_id, reason="strategy quarantined")
    row = jobs.get(job_id)
    assert row["status"] == "cancelled"
    assert row["error_message"] == "strategy quarantined"


def test_concurrent_claims_across_connections_are_exclusive(tmp_path):
    """The property `dispatch.py` depends on: N processes racing `claim()`
    against the same file must never double-claim a job. `BEGIN IMMEDIATE`
    plus `busy_timeout` (aqrl/db/connection.py) is what makes this safe."""
    db_path = tmp_path / "race.db"
    setup = connect(db_path)
    migrate(setup)
    setup_jobs = JobRepository(setup)
    job_ids = [setup_jobs.enqueue("EVALUATE") for _ in range(8)]
    setup.close()

    claimed_ids: list[int] = []
    lock = threading.Lock()

    def worker(worker_id: str) -> None:
        conn = connect(db_path)
        try:
            jobs = JobRepository(conn)
            while True:
                with transaction(conn, immediate=True):
                    job = jobs.claim(worker_id, lease_seconds=60)
                if job is None:
                    return
                with lock:
                    claimed_ids.append(job["id"])
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker, f"w{i}") for i in range(4)]
        for future in futures:
            future.result()

    assert sorted(claimed_ids) == sorted(job_ids), "every job claimed exactly once, none lost, none doubled"


def test_pending_count_and_recent_failures(conn, jobs):
    from aqrl.db.repositories import StrategyRepository

    strategy_id = StrategyRepository(conn).insert(name="n", family="f", market="m", timeframe="t")
    jobs.enqueue("EVALUATE")
    jobs.enqueue("EVALUATE", strategy_id=strategy_id, priority=1)  # claimed first
    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=60, job_types=["EVALUATE"])
    assert claimed["strategy_id"] == strategy_id

    jobs.fail(claimed["id"], "w1", error_message="boom", failure_class="deterministic")

    assert jobs.pending_count() == 1
    failures = jobs.recent_failures(strategy_id)
    assert len(failures) == 1
    assert failures[0]["error_message"] == "boom"

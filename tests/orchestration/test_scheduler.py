"""The tick loop — App-Flow §13 — and TRD §4.5 idle-cause reporting."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aqrl.db import transaction
from aqrl.db.repositories import JobRepository, StrategyRepository
from aqrl.orchestration.budgets import BudgetRepository
from aqrl.orchestration.dispatch import Dispatcher
from aqrl.orchestration.scheduler import (
    TIME_DRIVEN_SCHEDULE,
    TimeDrivenJob,
    diagnose_idle,
    fire_due_time_jobs,
    tick,
)


@pytest.fixture
def strategy_id(conn) -> int:
    return StrategyRepository(conn).insert(name="n", family="f", market="m", timeframe="t")


@pytest.fixture
def dispatcher(conn) -> Dispatcher:
    return Dispatcher(conn, worker_id="test-scheduler", max_concurrent=2)


# -- diagnose_idle ----------------------------------------------------------------


def test_diagnose_idle_reports_concurrency_cap(conn, dispatcher):
    dispatcher.max_concurrent = 0
    assert diagnose_idle(conn, dispatcher) == "concurrency_cap"


def test_diagnose_idle_reports_budget_exhausted(conn, dispatcher):
    BudgetRepository(conn).upsert("global", "experiments", "day", 0)
    assert diagnose_idle(conn, dispatcher).startswith("budget_exhausted")


def test_diagnose_idle_reports_queue_empty(conn, dispatcher):
    assert diagnose_idle(conn, dispatcher) == "queue_empty"


def test_diagnose_idle_reports_validation_flags(conn, dispatcher, strategy_id):
    from aqrl.db.repositories import SnapshotRepository, ValidationFlagRepository

    JobRepository(conn).enqueue("ARCHIVE", strategy_id=strategy_id)
    # A snapshot row is required by the FK; a minimal one is enough — this
    # test is about the flag, not the snapshot content.
    snapshot_id = SnapshotRepository(conn).insert(
        market="nse_equity", timeframe="daily", asset_class="cash_equity",
        period_start="2020-01-01", period_end="2020-01-02", storage_path="x",
        raw_content_hash="h" * 64, corporate_actions_version="v" * 64,
        adjustment_method="back_ratio_price",
    )
    ValidationFlagRepository(conn).insert(
        snapshot_id=snapshot_id, flag_type="gap", resolution="pending", created_at=datetime.now(UTC).isoformat()
    )

    assert diagnose_idle(conn, dispatcher) == "blocked_on_validation_flags"


def test_diagnose_idle_reports_schedule_or_dependency_block(conn, dispatcher, strategy_id):
    JobRepository(conn).enqueue("ARCHIVE", strategy_id=strategy_id, scheduled_for="2999-01-01T00:00:00+00:00")
    assert diagnose_idle(conn, dispatcher) == "blocked_on_schedule_or_dependencies"


# -- fire_due_time_jobs -----------------------------------------------------------


def test_fire_due_time_jobs_dedupes_within_the_same_period(conn):
    schedule = [TimeDrivenJob(name="nightly_report", job_type="GENERATE_REPORT", cadence="daily")]
    now = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)

    first = fire_due_time_jobs(conn, schedule, now=now)
    second = fire_due_time_jobs(conn, schedule, now=now.replace(hour=23))  # same day, later hour

    assert first == second
    assert JobRepository(conn).count(job_type="GENERATE_REPORT") == 1


def test_fire_due_time_jobs_fires_again_in_a_new_period(conn):
    schedule = [TimeDrivenJob(name="nightly_report", job_type="GENERATE_REPORT", cadence="daily")]
    day_one = fire_due_time_jobs(conn, schedule, now=datetime(2026, 7, 29, tzinfo=UTC))
    day_two = fire_due_time_jobs(conn, schedule, now=datetime(2026, 7, 30, tzinfo=UTC))

    assert day_one != day_two
    assert JobRepository(conn).count(job_type="GENERATE_REPORT") == 2


def test_fire_due_time_jobs_with_empty_schedule_is_a_noop(conn):
    assert fire_due_time_jobs(conn, [], now=datetime.now(UTC)) == []


def test_real_schedule_fires_weekly_mine_patterns(conn):
    """Stage 8's first real `TIME_DRIVEN_SCHEDULE` entry (App-Flow §8.2) —
    exercised against the actual module-level schedule, not a fixture copy,
    so a future edit to it is caught here."""
    fired = fire_due_time_jobs(conn, TIME_DRIVEN_SCHEDULE, now=datetime(2026, 8, 3, tzinfo=UTC))
    assert JobRepository(conn).count(job_type="MINE_PATTERNS") == 1
    assert len(fired) == 1

    # Same week, different day: dedupes (`_period_key`'s "weekly" cadence).
    fire_due_time_jobs(conn, TIME_DRIVEN_SCHEDULE, now=datetime(2026, 8, 4, tzinfo=UTC))
    assert JobRepository(conn).count(job_type="MINE_PATTERNS") == 1


# -- tick ---------------------------------------------------------------------


def test_tick_expires_leases_and_reports_the_orphan(conn, dispatcher, strategy_id):
    jobs = JobRepository(conn)
    job_id = jobs.enqueue("COLLECT_PAPERS", strategy_id=strategy_id)
    with transaction(conn, immediate=True):
        jobs.claim("some-dead-worker", lease_seconds=1)

    # `schedule=[]`: isolates this test from `TIME_DRIVEN_SCHEDULE`'s real
    # weekly `MINE_PATTERNS` entry (Stage 8) — this tick's reclaimed
    # `COLLECT_PAPERS` job would otherwise be dispatched alongside a real
    # `MINE_PATTERNS` job neither this test nor its fixtures are set up to
    # service (no `KnowledgeSession` installed process-wide).
    report = tick(conn, dispatcher, schedule=[], now=datetime(2999, 1, 1, tzinfo=UTC))
    assert job_id in report.expired_leases


def test_tick_with_nothing_queued_reports_queue_empty(conn, dispatcher):
    # Empty `schedule=[]`: this test is about dispatch with nothing pending,
    # not about the real `TIME_DRIVEN_SCHEDULE`'s weekly `MINE_PATTERNS`
    # entry (Stage 8) firing and creating something to dispatch — that is
    # `test_fire_due_time_jobs_*`'s concern, exercised in isolation below.
    report = tick(conn, dispatcher, schedule=[])
    assert report.dispatched == []
    assert report.idle_cause == "queue_empty"


def test_tick_dispatches_and_reaps_a_fast_failing_job(conn, dispatcher, strategy_id):
    job_id = JobRepository(conn).enqueue("COLLECT_PAPERS", strategy_id=strategy_id)
    report = tick(conn, dispatcher, schedule=[])
    assert report.dispatched == [job_id]

    for running in dispatcher._running.values():
        running.popen.wait(timeout=15)
    report2 = tick(conn, dispatcher, schedule=[])
    assert job_id in report2.reaped

    assert JobRepository(conn).get(job_id)["status"] == "failed"

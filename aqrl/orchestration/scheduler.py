"""The one always-on tick loop (App-Flow §13).

```
every ~60 seconds:
        expire dead leases -> return orphaned jobs to pending
        check budgets -> back-pressure gates dispatch, not a separate flag store
        fire due time-based jobs
        select pending jobs, respecting concurrency + budget caps, dispatch
        reap completed workers, record cost + duration
```

**Restart safety is the point.** Nothing here keeps state outside the
database except which subprocesses *this* scheduler process spawned — an
optimisation `dispatch.Dispatcher` uses to reap quickly, never a source of
truth. Killing the scheduler loses nothing: claimed jobs' leases expire, the
next scheduler (or the next tick of this one, after a restart) reclaims them.

**Idle-cause reporting (TRD §4.5)** is not an afterthought bolted on after
the loop — it is the return value of every tick that dispatched nothing, so
"the lab is quiet" is never ambiguous between *healthy and throttled* and
*broken and stalled*.
"""
from __future__ import annotations

import signal
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..db import transaction
from ..db.repositories import DeploymentRepository, JobRepository, ResearchGoalRepository
from ..db.repositories.base import Row
from ..logging import get_logger
from .budgets import check_global
from .dispatch import Dispatcher

__all__ = [
    "ALLOCATION_BUCKET_WEIGHTS",
    "TIME_DRIVEN_SCHEDULE",
    "TickReport",
    "TimeDrivenJob",
    "diagnose_idle",
    "fire_due_hypothesis_batch",
    "fire_due_monitor_batch",
    "fire_due_time_jobs",
    "run_forever",
    "tick",
]

#: PRD §4.5's 70/20/10 split, as relative weights for the nightly batch's
#: weighted round-robin (below) — order, not selection: every eligible goal
#: is still enqueued every day, this only decides which bucket's
#: `GENERATE_SPEC` jobs the scheduler tends to claim first when several
#: compete in one tick. `hypothesis_budget`/`hypotheses_used` per goal
#: remains the real, durable cap (Backend-Schema §3).
ALLOCATION_BUCKET_WEIGHTS: dict[str, int] = {"incremental": 7, "cross_market": 2, "exploratory": 1}

_log = get_logger(__name__)


@dataclass(frozen=True)
class TimeDrivenJob:
    """One entry in TRD §4.2's cadence table."""

    name: str
    job_type: str
    cadence: str  # "hourly" | "daily" | "weekly" | "weekend"
    payload: dict = field(default_factory=dict)


#: TRD §4.2's cadence table, encoded. `MINE_PATTERNS` is Stage 8's entry —
#: App-Flow §8.2's weekly cross-experiment pattern mining. `collect_papers`
#: and `collect_market_data` are Stage 10's — App-Flow §12's collector
#: cadence table (`arXiv/SSRN`/blogs hourly, market data daily
#: post-close), now that `handlers/librarian.py` exists to service them.
#: `COLLECT_GITHUB` has no handler yet and stays out of this list for the
#: same reason every prior stage's still-unimplemented job types did: a
#: schedule entry with nothing to service it just fails immediately
#: (`NotImplementedHandler`), which is worse than not queuing at all.
TIME_DRIVEN_SCHEDULE: list[TimeDrivenJob] = [
    TimeDrivenJob("mine_patterns", "MINE_PATTERNS", "weekly"),
    TimeDrivenJob("collect_papers", "COLLECT_PAPERS", "hourly"),
    TimeDrivenJob("collect_market_data", "COLLECT_MARKET_DATA", "daily"),
]


@dataclass(frozen=True)
class TickReport:
    expired_leases: list[int]
    time_jobs_fired: list[int]
    dispatched: list[int]
    terminated_for_timeout: list[int]
    reaped: list[int]
    idle_cause: str | None
    hypothesis_jobs_fired: list[int] = field(default_factory=list)
    monitor_jobs_fired: list[int] = field(default_factory=list)


def _period_key(cadence: str, now: datetime) -> str:
    if cadence == "hourly":
        return now.strftime("%Y-%m-%dT%H")
    if cadence == "daily":
        return now.strftime("%Y-%m-%d")
    if cadence == "weekly":
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if cadence == "weekend":
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}-weekend"
    raise ValueError(f"unknown cadence {cadence!r}")


def fire_due_time_jobs(
    conn: sqlite3.Connection, schedule: list[TimeDrivenJob] | None = None, *, now: datetime | None = None
) -> list[int]:
    """Enqueue each schedule entry due this period. Idempotent per period via
    `dedupe_key` — safe to call every tick even if ticks overlap."""
    schedule = TIME_DRIVEN_SCHEDULE if schedule is None else schedule
    if not schedule:
        return []
    now = now or datetime.now(UTC)
    jobs = JobRepository(conn)
    fired: list[int] = []
    with transaction(conn, immediate=True):
        for entry in schedule:
            key = f"time:{entry.name}:{_period_key(entry.cadence, now)}"
            fired.append(jobs.enqueue(entry.job_type, entry.payload, dedupe_key=key))
    return fired


def _weighted_round_robin(buckets: dict[str, list[Row]], weights: dict[str, int]) -> list[Row]:
    """Merge each bucket's rows into one order, interleaved proportionally
    to `weights` (surplus/credit round-robin: every bucket's credit grows by
    its weight each round; the highest-credit non-empty bucket goes next and
    is debited by the total weight) — so a 7:2:1 split spreads roughly
    `I I C I I C I I E I I C ...`, not `IIIIIII CC E`."""
    total_weight = sum(weights.get(b, 1) for b in buckets) or 1
    indices = {bucket: 0 for bucket in buckets}
    credits = {bucket: 0 for bucket in buckets}
    order: list[Row] = []
    remaining = sum(len(rows) for rows in buckets.values())
    while remaining > 0:
        for bucket in buckets:
            credits[bucket] += weights.get(bucket, 1)
        eligible = [b for b in buckets if indices[b] < len(buckets[b])]
        chosen = max(eligible, key=lambda b: credits[b])
        order.append(buckets[chosen][indices[chosen]])
        indices[chosen] += 1
        credits[chosen] -= total_weight
        remaining -= 1
    return order


def fire_due_hypothesis_batch(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[int]:
    """A1's nightly batch trigger (Implementation_Plan §10, App-Flow §3.1) —
    one `GENERATE_SPEC` job per active `research_goals` row with unused
    hypothesis budget. Not folded into `TIME_DRIVEN_SCHEDULE`, since that
    list fires one fixed job/payload per entry and this needs one job per
    *row* found at tick time.

    Idempotent per goal per day via `dedupe_key`, the same pattern
    `fire_due_time_jobs` already uses — an overlapping tick never
    double-fires the same goal. Enqueue order follows
    `ALLOCATION_BUCKET_WEIGHTS`'s 70/20/10 split (PRD §4.5) via
    `_weighted_round_robin`; every eligible goal still fires today, the
    split only orders which bucket's jobs `JobRepository.claim` (highest
    `priority` first) tends to pick up first when more than one is pending
    at once.
    """
    now = now or datetime.now(UTC)
    goals = ResearchGoalRepository(conn).active_with_budget()
    if not goals:
        return []

    buckets: dict[str, list[Row]] = {bucket: [] for bucket in ALLOCATION_BUCKET_WEIGHTS}
    for goal in goals:
        bucket = goal.get("allocation_bucket")
        buckets.setdefault(bucket if bucket in ALLOCATION_BUCKET_WEIGHTS else "exploratory", []).append(goal)

    ordered = _weighted_round_robin(buckets, ALLOCATION_BUCKET_WEIGHTS)
    date_key = now.strftime("%Y-%m-%d")

    jobs = JobRepository(conn)
    fired: list[int] = []
    with transaction(conn, immediate=True):
        for priority, goal in enumerate(reversed(ordered)):
            key = f"generate_spec:{goal['id']}:{date_key}"
            fired.append(
                jobs.enqueue(
                    "GENERATE_SPEC",
                    {"goal_id": goal["id"]},
                    dedupe_key=key,
                    priority=priority,
                )
            )
    return fired


def fire_due_monitor_batch(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[int]:
    """Stage 11's daily health check trigger (Implementation_Plan §14,
    App-Flow §10) — one `MONITOR_DEPLOYMENT` job per `deployments.status =
    'active'` row. Same shape as `fire_due_hypothesis_batch` above and for
    the same reason: `TIME_DRIVEN_SCHEDULE` fires one fixed job per entry
    with no id to key on, and this needs one job per *row* found at tick
    time.

    Idempotent per deployment per day via `dedupe_key` — an overlapping tick
    never double-checks the same deployment. Deliberately not derived from
    `emit(Event.PAPER_TRADING_MILESTONE)`'s per-entity default key
    (`events.py`'s own warning): that key is `f"{event}:{deployment_id}"`
    with no date component, which would collapse every day's check into the
    same job forever.
    """
    now = now or datetime.now(UTC)
    deployments = DeploymentRepository(conn).active()
    if not deployments:
        return []

    date_key = now.strftime("%Y-%m-%d")
    jobs = JobRepository(conn)
    fired: list[int] = []
    with transaction(conn, immediate=True):
        for deployment in deployments:
            key = f"monitor:{deployment['id']}:{date_key}"
            fired.append(
                jobs.enqueue(
                    "MONITOR_DEPLOYMENT",
                    {"deployment_id": deployment["id"]},
                    strategy_id=deployment["strategy_id"],
                    dedupe_key=key,
                )
            )
    return fired


def diagnose_idle(conn: sqlite3.Connection, dispatcher: Dispatcher) -> str:
    """Why did this tick dispatch nothing? TRD §4.5's table, evaluated in
    priority order — the first true condition is reported."""
    if dispatcher.running_count >= dispatcher.max_concurrent:
        return "concurrency_cap"

    back_pressure = check_global(conn)
    if not back_pressure.allowed:
        return back_pressure.reason or "budget_exhausted"

    jobs = JobRepository(conn)
    pending = jobs.pending_count()
    if pending == 0:
        # TRD §4.5: "queue empty, budget available, no limit hit" is the one
        # idle cause the table calls a bug, not a rest state — logged as such
        # by the caller, never silently treated as healthy.
        return "queue_empty"

    # Approximation, not a precise join to the stuck jobs: any unresolved
    # flag anywhere is reported as the cause once nothing else explains the
    # stall. A tighter version would trace each pending job back to the
    # snapshot(s) it needs and check only those — worth doing once idle
    # causes are surfaced somewhere a human actually watches (Stage 4a).
    unresolved_flags = conn.execute(
        "SELECT COUNT(*) AS n FROM data_validation_flags WHERE resolution = 'pending'"
    ).fetchone()["n"]
    if unresolved_flags:
        return "blocked_on_validation_flags"

    # Pending work exists, nothing is claimable: everything due is either on
    # a retry backoff (`scheduled_for` in the future) or waiting on a
    # dependency that has not succeeded yet.
    return "blocked_on_schedule_or_dependencies"


def tick(
    conn: sqlite3.Connection,
    dispatcher: Dispatcher,
    *,
    schedule: list[TimeDrivenJob] | None = None,
    now: datetime | None = None,
) -> TickReport:
    """One pass of the loop. Safe to call repeatedly; every step is either
    idempotent or operates only on rows genuinely due."""
    now = now or datetime.now(UTC)
    jobs = JobRepository(conn)

    with transaction(conn, immediate=True):
        expired = jobs.expire_leases(now=now.isoformat())

    time_jobs = fire_due_time_jobs(conn, schedule, now=now)
    hypothesis_jobs = fire_due_hypothesis_batch(conn, now=now)
    monitor_jobs = fire_due_monitor_batch(conn, now=now)
    dispatched = dispatcher.dispatch_pending()
    terminated = dispatcher.enforce_time_budgets()
    reaped = dispatcher.reap()

    idle_cause = None
    if not dispatched and dispatcher.running_count == 0:
        idle_cause = diagnose_idle(conn, dispatcher)
        level = _log.warning if idle_cause in ("queue_empty",) else _log.info
        level("scheduler idle", extra={"idle_cause": idle_cause})

    return TickReport(
        expired, time_jobs, dispatched, terminated, reaped, idle_cause, hypothesis_jobs, monitor_jobs
    )


def run_forever(
    conn: sqlite3.Connection,
    dispatcher: Dispatcher,
    *,
    tick_seconds: int | None = None,
    schedule: list[TimeDrivenJob] | None = None,
    max_ticks: int | None = None,
) -> None:
    """The always-on loop. `max_ticks` exists only for tests — production
    callers omit it and rely on `SIGINT`/`SIGTERM` for graceful shutdown.

    Shutdown does not kill running workers: it stops claiming new jobs and
    returns once the signal is observed, so the caller can wait on already-
    dispatched subprocesses (or simply exit — their leases expire and the
    next scheduler reclaims them, same as any other crash).
    """
    from ..config import get_settings

    interval = tick_seconds or get_settings().scheduler_tick_seconds
    stop = {"requested": False}

    def _handle_signal(signum, frame) -> None:  # noqa: ANN001 - signal handler signature
        _log.info("shutdown requested", extra={"signal": signum})
        stop["requested"] = True

    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, _handle_signal)

    ticks = 0
    try:
        while not stop["requested"]:
            report = tick(conn, dispatcher, schedule=schedule)
            _log.debug("tick complete", extra={"report": report})
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            if not stop["requested"]:
                time.sleep(interval)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)

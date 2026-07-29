"""The nervous system (Implementation_Plan §6, TRD §4) — Stage 4.

Every arrow between agents passes through the `jobs` table and the scheduler
(App-Flow §3): no agent ever calls another agent directly. A worker writes a
row; `events.emit` turns that write into the next job; the scheduler notices
pending work and dispatches it. This package is the code behind that shape:

    queue.py       atomic claim/heartbeat/complete over `jobs` — TRD §4.3
    states.py      the strategy/experiment/job state machines — invalid
                   transitions are errors, not warnings
    events.py      the TRD §4.1 event -> job_type table, `emit()` glues a
                   state transition and an enqueue into one transaction
    budgets.py     back-pressure over the `budgets` table — TRD §4.4
    failures.py    transient/deterministic classification, retry backoff,
                   quarantine after k consecutive failures — TRD §4.3
    worker.py      one job, one subprocess; dispatches to `handlers/`
    dispatch.py    spawns and reaps worker subprocesses, enforces the
                   per-experiment time budget — TRD §9.6
    scheduler.py   the one always-on tick loop — App-Flow §13

Stage 4's done-when is a crash test, not a feature list: "the scheduler
survives `kill -9` mid-job with zero state loss and zero duplicated work."
Every module above earns its place by contributing to that guarantee.
"""
from __future__ import annotations

"""The TRD §4.1 event -> job_type table, encoded as data.

**"A database write is the trigger"** (TRD §4.1) — no agent calls another
agent. A worker (or a human, via the CLI) writes a row that means something
happened; `emit()` turns that meaning into the next job, in the same
transaction as whatever state change produced it. That single-transaction
property is half of Stage 4's done-when: there is never a window where a
state advanced but its follow-on job was lost, or a job exists for a state
change that never actually committed.

`emit()` does **not** open its own transaction — the caller (a job handler, a
CLI command) wraps the state change and the emit in one
`with transaction(conn, immediate=True):` block, exactly like
`states.transition()`. That is what makes "persist the evaluation and enqueue
PROMOTE/REVIEW" atomic in `handlers/evaluate.py`.

Not every TRD §4.1 event maps to a `jobs` row — a few are direct actions
(a human approval merges a branch; a red health check is a dashboard
notification) rather than queued work. Those are declared here with a `None`
job type so the full table is checkable against TRD even before every
producer exists — later stages wire the ones Stage 4 doesn't fire.
"""
from __future__ import annotations

import sqlite3
from enum import Enum
from typing import Any

from ..db.repositories import AuditLogRepository, JobRepository

__all__ = ["EVENT_JOB_TYPE", "Event", "UnknownEvent", "emit"]


class Event(str, Enum):
    SPEC_SAVED = "spec_saved"
    CODE_CHECKS_PASSED = "code_checks_passed"
    CODE_CHECKS_FAILED = "code_checks_failed"
    EVALUATION_CLEARED_BAR = "evaluation_cleared_bar"
    EVALUATION_FAILED_BAR = "evaluation_failed_bar"
    REVIEW_ITERATE = "review_iterate"
    REVIEW_PLATEAU_OR_REJECT = "review_plateau_or_reject"
    PROMOTION_DECIDED = "promotion_decided"
    HUMAN_APPROVED_GATE = "human_approved_gate"
    PAPER_TRADING_MILESTONE = "paper_trading_milestone"
    HEALTH_CHECK_RED = "health_check_red"
    DOCUMENT_INGESTED = "document_ingested"
    HIGH_NOVELTY_EXTRACTION = "high_novelty_extraction"
    FAILURE_PATTERN_DETECTED = "failure_pattern_detected"
    DATA_SNAPSHOT_FLAGGED = "data_snapshot_flagged"


#: TRD §4.1, verbatim. `None` = a direct action, not a queued job (see module
#: docstring). Stage 4 only ever *fires* `CODE_CHECKS_PASSED/FAILED` and
#: `EVALUATION_CLEARED_BAR/FAILED_BAR` — the rest are producers Stage 5+ adds,
#: kept here so the table is complete and testable against TRD from day one.
EVENT_JOB_TYPE: dict[Event, str | None] = {
    Event.SPEC_SAVED: "IMPLEMENT",
    Event.CODE_CHECKS_PASSED: "EVALUATE",
    Event.CODE_CHECKS_FAILED: "FIX_CODE",
    Event.EVALUATION_CLEARED_BAR: "PROMOTE",
    Event.EVALUATION_FAILED_BAR: "REVIEW",
    Event.REVIEW_ITERATE: "IMPLEMENT",
    Event.REVIEW_PLATEAU_OR_REJECT: "ARCHIVE",
    Event.PROMOTION_DECIDED: "ARCHIVE",
    Event.HUMAN_APPROVED_GATE: None,  # creates a deployment + merges the branch directly (TRD §5.3)
    Event.PAPER_TRADING_MILESTONE: "MONITOR_DEPLOYMENT",
    Event.HEALTH_CHECK_RED: None,  # lifecycle action + dashboard notification, not a job (Stage 11)
    Event.DOCUMENT_INGESTED: "EXTRACT_KNOWLEDGE",
    Event.HIGH_NOVELTY_EXTRACTION: "GENERATE_SPEC",
    Event.FAILURE_PATTERN_DETECTED: None,  # pushes a research_questions row (Stage 8), not a job
    Event.DATA_SNAPSHOT_FLAGGED: None,  # notifies a human; the snapshot is unusable until resolved
}


class UnknownEvent(ValueError):
    pass


def emit(
    conn: sqlite3.Connection,
    event: Event,
    *,
    strategy_id: int | None = None,
    experiment_id: int | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
    actor: str = "system",
    reasoning: str | None = None,
) -> int | None:
    """Record the event and, if it maps to one, enqueue the follow-on job.

    Returns the new job's id, or `None` for an event whose job type is not
    yet wired (or genuinely has none). Must run inside a transaction the
    caller controls — see the module docstring.
    """
    if event not in EVENT_JOB_TYPE:
        raise UnknownEvent(f"unregistered event {event!r}")
    job_type = EVENT_JOB_TYPE[event]

    AuditLogRepository(conn).record(
        actor=actor,
        action=f"event:{event.value}",
        entity_type="experiments" if experiment_id is not None else ("strategies" if strategy_id is not None else None),
        entity_id=experiment_id if experiment_id is not None else strategy_id,
        reasoning=reasoning,
        evidence=payload,
    )

    if job_type is None:
        return None

    key = dedupe_key or f"{event.value}:{experiment_id if experiment_id is not None else strategy_id}"
    return JobRepository(conn).enqueue(
        job_type,
        payload or {},
        strategy_id=strategy_id,
        experiment_id=experiment_id,
        priority=priority,
        dedupe_key=key,
        max_attempts=max_attempts,
    )

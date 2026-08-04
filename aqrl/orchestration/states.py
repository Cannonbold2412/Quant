"""State machines for `strategies.status` and `experiments.status`.

Implementation_Plan §6 is explicit: **"invalid transitions are errors, not
warnings."** So `transition()` looks the move up in a table and raises
`InvalidTransition` rather than logging and applying it anyway — a caller
that reaches an impossible state has a bug, and the loudest possible failure
is the correct one. Every transition that *is* applied is recorded in
`audit_log` (Backend-Schema §13), so "why is this strategy `quarantined`?" is
always a query, never a memory.

The tables encode Backend-Schema §14.1/§14.2 exactly. `jobs.status` (§14.3)
is deliberately not modelled here — `JobRepository`'s methods (`claim`,
`succeed`, `fail`, ...) already enforce its transitions via their `WHERE
status IN (...)` clauses, which is a tighter guarantee than a lookup table
would add.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..db.repositories import AuditLogRepository, ExperimentRepository, StrategyRepository

__all__ = ["EXPERIMENT_TRANSITIONS", "STRATEGY_TRANSITIONS", "InvalidTransition", "transition"]

#: Backend-Schema §14.1. `iterating` loops back to `coding` — below the bar
#: only; clearing the bar is a one-way trip toward promotion.
#: Poison-pill protection (`failures.maybe_quarantine`) can fire while jobs
#: are outstanding against a strategy in almost any non-terminal state, not
#: just the "obvious" `coding`/`evaluating` ones — a repeatedly-failing
#: `PROMOTE` job while `pending_promotion`, say. `quarantined` is therefore
#: added to every state that isn't already terminal or itself `quarantined`,
#: so the safety mechanism can never itself raise `InvalidTransition`.
STRATEGY_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"spec_ready", "quarantined"}),
    "spec_ready": frozenset({"coding", "quarantined"}),
    "coding": frozenset({"evaluating", "quarantined"}),
    "evaluating": frozenset({"evaluated", "quarantined"}),
    "evaluated": frozenset({"iterating", "pending_promotion", "plateaued", "rejected", "quarantined"}),
    "iterating": frozenset({"coding", "quarantined"}),
    "plateaued": frozenset({"retired"}),
    "rejected": frozenset({"retired"}),
    # `rejected` here is a deliberate deviation from Backend-Schema §14.1,
    # which only draws that edge off the never-cleared-the-bar branch. A4
    # (Stage 8) can reject a bar-clearing strategy on its own authority
    # (App-Flow §7.3) with nowhere else for it to land — the same
    # closest-available-bucket precedent Stages 3/5/6 already set for gaps
    # this specific. `defer` deliberately leaves status untouched: PRD §9.2
    # already stopped this strategy the instant it cleared the bar, so
    # deferring reopens research via a fresh `research_goals` row
    # (`handlers/promote.py`), not by resuming this strategy's own loop.
    "pending_promotion": frozenset({"awaiting_human_review", "rejected", "quarantined"}),
    "awaiting_human_review": frozenset({"paper_trading", "rejected", "quarantined"}),
    "paper_trading": frozenset({"pending_live_review", "retired", "quarantined"}),
    "pending_live_review": frozenset({"live_small", "paper_trading", "retired", "quarantined"}),
    "live_small": frozenset({"live_scaled", "retired", "quarantined"}),
    "live_scaled": frozenset({"retired", "quarantined"}),
    "retired": frozenset(),
    "quarantined": frozenset({"coding", "retired"}),  # human-inspected, then resumed or retired
}

#: Backend-Schema §14.2.
EXPERIMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"code_pending", "error"}),
    "code_pending": frozenset({"code_ready", "failed", "error"}),
    "code_ready": frozenset({"evaluating", "error"}),
    "evaluating": frozenset({"evaluated", "failed", "error"}),
    "evaluated": frozenset({"reviewed", "archived"}),
    "reviewed": frozenset({"archived"}),
    "archived": frozenset(),
    "failed": frozenset({"archived"}),
    "error": frozenset({"archived"}),
}

_TABLES: dict[str, dict[str, frozenset[str]]] = {
    "strategies": STRATEGY_TRANSITIONS,
    "experiments": EXPERIMENT_TRANSITIONS,
}
_REPOS: dict[str, type] = {"strategies": StrategyRepository, "experiments": ExperimentRepository}


class InvalidTransition(Exception):
    """Raised, never logged-and-continued (Implementation_Plan §6)."""

    def __init__(self, table: str, entity_id: int, from_status: str, to_status: str) -> None:
        self.table = table
        self.entity_id = entity_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"{table} {entity_id}: {from_status!r} -> {to_status!r} is not a valid transition")


def transition(
    conn: sqlite3.Connection,
    table: str,
    entity_id: int,
    to: str,
    *,
    actor: str = "system",
    reasoning: str | None = None,
    evidence: dict[str, Any] | None = None,
    **extra_fields: Any,
) -> None:
    """Move `table`'s row `entity_id` to status `to`, or raise.

    `extra_fields` lets a caller update other columns in the same write as the
    status change (e.g. quarantine sets `quarantined` and `quarantine_reason`
    alongside `status='quarantined'`) — one UPDATE, one audit row, not two.

    Must be called inside a transaction the caller controls — this does not
    open its own, so it composes with `events.emit()`'s single-transaction
    state-change-plus-enqueue guarantee.
    """
    try:
        transitions = _TABLES[table]
        repo_cls = _REPOS[table]
    except KeyError:
        raise ValueError(f"no state machine registered for table {table!r}") from None

    repo = repo_cls(conn)
    row = repo.get(entity_id)
    if row is None:
        raise KeyError(f"no {table} row {entity_id}")

    current = row["status"]
    if to not in transitions.get(current, frozenset()):
        raise InvalidTransition(table, entity_id, current, to)

    repo.update(entity_id, status=to, **extra_fields)
    AuditLogRepository(conn).record(
        actor=actor,
        action=f"{table}.status: {current} -> {to}",
        entity_type=table,
        entity_id=entity_id,
        reasoning=reasoning,
        evidence=evidence,
    )

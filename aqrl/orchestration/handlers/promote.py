"""The `PROMOTE` handler — Stage 8's A4 (Implementation_Plan §11).

**Trigger:** the instant an evaluation clears the bar (App-Flow §7) —
`handlers/evaluate.py`'s bar-clear short-circuit emits `Event.
EVALUATION_CLEARED_BAR`, enqueuing this job directly. A3 need not have run
at all; A4 reads the winning evaluation on its own.

**A recommendation, never an action.** `persist()` always writes
`requires_human_approval=1` regardless of what A4 decided — App-Flow §7.3:
*"A4 can say no alone, never yes alone."* Nothing here merges a branch,
moves capital, or grants execution authority; that is Stage 9's `aqrl
review` gate (TRD §5.3).

**Same `run`/`persist` split as `review.py`/`implement.py`**, for the same
reason: a multi-minute A4 call must never hold SQLite's write lock.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ...agents.context import assemble_promote_brief, promote_prompt_version
from ...agents.session import PromotionSession
from ...db.repositories import (
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    PromotionRepository,
    ResearchGoalRepository,
    SpecRepository,
    StrategyRepository,
)
from ...db.repositories.base import Row
from ..budgets import consume as budget_consume
from ..events import Event, emit
from ..states import transition
from .base import HandlerResult

__all__ = ["PromoteOutcome", "persist", "run", "set_session"]

_session_override: PromotionSession | None = None


def set_session(session: PromotionSession | None) -> None:
    """Override the session `decide()` calls. Tests only.

    `None` restores the default — a lazily-constructed `AnthropicSession`, so
    importing or calling this handler never requires `ANTHROPIC_API_KEY`
    unless a real (non-stale) promotion decision actually happens.
    """
    global _session_override
    _session_override = session


def _get_session() -> PromotionSession:
    if _session_override is not None:
        return _session_override
    from ...agents.session import AnthropicSession

    return AnthropicSession()


@dataclass(frozen=True)
class PromoteOutcome:
    stale: bool
    strategy_id: int
    winning_experiment_id: int
    decision: str | None = None  # "approve" | "reject" | "defer"; None only when stale
    rationale: str | None = None
    evidence_summary: dict[str, Any] = field(default_factory=dict)
    overfitting_risk: str | None = None
    confidence: float | None = None
    capacity_liquidity_ok: bool | None = None
    recommended_allocation_pct: float | None = None
    prompt_version: str | None = None
    tokens_spent: int = 0
    iterations_considered: int = 0
    strategy_snapshot: Row | None = None


def run(conn: sqlite3.Connection, job: Row) -> PromoteOutcome:
    """The heavy, side-effect-free-on-the-database half. Writes nothing —
    every insert and state transition happens in `persist()`."""
    strategy_id = job["strategy_id"]
    if strategy_id is None:
        raise ValueError("PROMOTE job has no strategy_id")

    strategies = StrategyRepository(conn)
    strategy = strategies.get(strategy_id)
    if strategy is None:
        raise ValueError(f"no strategy {strategy_id}")

    winning_experiment_id = strategy.get("best_experiment_id")
    if winning_experiment_id is None:
        raise ValueError(f"strategy {strategy_id} has no best_experiment_id to promote")

    # Guard: a race (this job already handled, or superseded by a re-run) is
    # a no-op, not an error — same shape as `review.py`'s stale guard.
    already_decided = PromotionRepository(conn).latest_for_strategy(strategy_id) is not None
    if strategy["status"] != "pending_promotion" or already_decided:
        return PromoteOutcome(stale=True, strategy_id=strategy_id, winning_experiment_id=winning_experiment_id)

    experiments = ExperimentRepository(conn)
    experiment = experiments.get(winning_experiment_id)
    if experiment is None:
        raise ValueError(f"no experiment {winning_experiment_id}")

    spec = SpecRepository(conn).load_spec(experiment["spec_id"])
    experiment_history = experiments.find(strategy_id=strategy_id, order_by="iteration")
    winning_evaluation = EvaluationRepository(conn).latest_for_experiment(winning_experiment_id)
    if winning_evaluation is None:
        raise ValueError(f"experiment {winning_experiment_id} has no evaluation to promote")
    diagnostic_checks = EvaluationTestRepository(conn).for_evaluation(winning_evaluation["id"])

    brief = assemble_promote_brief(
        strategy=strategy,
        spec=spec,
        experiment_history=experiment_history,
        winning_evaluation=winning_evaluation,
        diagnostic_checks=diagnostic_checks,
        iteration_count=strategy["iteration_count"],
    )
    prompt_version = promote_prompt_version()
    response = _get_session().decide(brief, prompt_version=prompt_version)
    promotion = response.promotion

    return PromoteOutcome(
        stale=False,
        strategy_id=strategy_id,
        winning_experiment_id=winning_experiment_id,
        decision=promotion.decision,
        rationale=promotion.rationale,
        evidence_summary=promotion.evidence_summary,
        overfitting_risk=promotion.overfitting_risk,
        confidence=promotion.confidence,
        capacity_liquidity_ok=promotion.capacity_liquidity_ok,
        recommended_allocation_pct=promotion.recommended_allocation_pct,
        prompt_version=prompt_version,
        tokens_spent=response.tokens_spent,
        iterations_considered=strategy["iteration_count"],
        strategy_snapshot=strategy,
    )


def persist(conn: sqlite3.Connection, job: Row, outcome: PromoteOutcome) -> HandlerResult:
    """The short, atomic half: every database write, in one transaction."""
    if outcome.stale:
        return HandlerResult(tokens_spent=0)

    promotions = PromotionRepository(conn)
    promotion_id = promotions.insert(
        strategy_id=outcome.strategy_id,
        best_experiment_id=outcome.winning_experiment_id,
        stage_from="research",
        stage_to="human_review",
        decision=outcome.decision,
        rationale=outcome.rationale,
        evidence_summary=outcome.evidence_summary,
        iterations_considered=outcome.iterations_considered,
        overfitting_risk=outcome.overfitting_risk,
        confidence=outcome.confidence,
        capacity_liquidity_ok=outcome.capacity_liquidity_ok,
        recommended_allocation_pct=outcome.recommended_allocation_pct,
        # Always 1 — App-Flow §7.3: A4 can say no alone, never yes alone. The
        # human's own verdict starts unresolved regardless of what A4 decided.
        requires_human_approval=True,
        human_decision="pending",
        prompt_version=outcome.prompt_version,
    )

    if outcome.decision == "approve":
        transition(conn, "strategies", outcome.strategy_id, "awaiting_human_review", actor="agent:A4")
    elif outcome.decision == "reject":
        transition(
            conn, "strategies", outcome.strategy_id, "rejected", actor="agent:A4", reasoning=outcome.rationale
        )
    else:  # "defer" — status untouched; the idea re-enters research via a new goal
        strategy = outcome.strategy_snapshot or StrategyRepository(conn).get(outcome.strategy_id)
        ResearchGoalRepository(conn).insert(
            title=f"Deferred by A4: {strategy.get('name')}",
            description=outcome.rationale,
            market=strategy.get("market"),
            timeframe=strategy.get("timeframe"),
            allocation_bucket="incremental",
            status="active",
            # Neither 'human' nor 'agent5' (research_goals.created_by's only
            # two values) fits A4 — left NULL, the same closest-available-
            # bucket tradeoff prior stages already took for gaps this specific.
            created_by=None,
        )

    # Every decision emits ARCHIVE — the lab learns from an approval as much
    # as from a rejection (App-Flow §8: "for EVERY experiment, promoted or
    # rejected").
    emit(
        conn,
        Event.PROMOTION_DECIDED,
        strategy_id=outcome.strategy_id,
        experiment_id=outcome.winning_experiment_id,
        payload={"promotion_id": promotion_id, "decision": outcome.decision},
    )

    budget_consume(conn, "strategy", "tokens", "day", outcome.tokens_spent, scope_id=outcome.strategy_id)
    return HandlerResult(tokens_spent=outcome.tokens_spent)

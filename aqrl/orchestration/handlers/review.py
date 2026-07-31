"""The `REVIEW` handler — Stage 6's A3 (Implementation_Plan §9).

**Only ever invoked below the bar** (App-Flow §6.1) — the worker checks
`bar_result` in `handlers/evaluate.py` before a `REVIEW` job is even enqueued,
and this handler's own guard (`run()`, below) re-checks it: a stale `REVIEW`
racing a bar-clear elsewhere is a no-op, not an error, and A3 is never
consulted about whether to keep pushing past a cleared bar (PRD §10.2 —
that would let an LLM quietly override satisficing, one plausible-sounding
justification at a time).

**Stop conditions are checked in Python before Claude is ever invoked**
(App-Flow §6.2's table) — plateau patience, the hard iteration cap, and now
the per-strategy budget, which this handler is the first real consumer of
(`aqrl.orchestration.budgets` tracked these rows since Stage 4 but nothing
spent against them until there was a caller). Any of the three forces a
`plateau` verdict with zero tokens spent — budget is never burned to be told
what Python already knew.

**Below the bar, only two things can happen next**: `iterate` loops back to
A2 (`emit(Event.REVIEW_ITERATE, ...)`, serviced by `implement.py`'s existing
`plan_iteration` path, unchanged), or `plateau`/`reject` end the strategy's
research and hand off to A5 (`emit(Event.REVIEW_PLATEAU_OR_REJECT, ...)` ->
`ARCHIVE`). A5 is Stage 8 and does not exist yet — exactly the same
"unimplemented job type fails loudly" situation Stage 5's `PROMOTE` job is
already in, and for the same reason this module does not stub it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ...agents.context import assemble_review_brief, review_prompt_version
from ...agents.session import ReviewSession
from ...db.repositories import (
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    KnowledgeEntryRepository,
    RegimePerformanceRepository,
    ResearchPlanRepository,
    SpecRepository,
    StrategyRepository,
)
from ...db.repositories.base import Row
from ...eval.bar import AcceptanceBar
from ..budgets import check as budget_check
from ..budgets import consume as budget_consume
from ..events import Event, emit
from ..states import transition
from .base import HandlerResult

__all__ = ["ReviewOutcome", "persist", "run", "set_session"]

#: Payload keys this handler consumes for itself — everything else (the
#: eval-relevant fields `implement.py`'s next `EVALUATE` will need) is
#: carried through unchanged, same convention as `implement.py`'s own
#: `_HANDLER_ONLY_PAYLOAD_KEYS`.
_HANDLER_ONLY_PAYLOAD_KEYS = frozenset({"evaluation_id"})

_session_override: ReviewSession | None = None


def set_session(session: ReviewSession | None) -> None:
    """Override the session `review()` calls. Tests only.

    `None` restores the default — a lazily-constructed `AnthropicSession`, so
    importing or calling this handler never requires `ANTHROPIC_API_KEY`
    unless a real (non-forced-stop) review actually happens.
    """
    global _session_override
    _session_override = session


def _get_session() -> ReviewSession:
    if _session_override is not None:
        return _session_override
    from ...agents.session import AnthropicSession

    return AnthropicSession()


@dataclass(frozen=True)
class ReviewOutcome:
    stale: bool
    strategy_id: int
    reviewed_experiment_id: int
    evaluation_id: int
    verdict: str | None = None  # "iterate" | "plateau" | "reject"; None only when stale
    diagnosis: str | None = None
    evidence_cited: list[dict[str, Any]] = field(default_factory=list)
    proposed_changes: list[dict[str, Any]] = field(default_factory=list)
    expected_effect: str | None = None
    confidence: float | None = None
    prompt_version: str | None = None
    tokens_spent: int = 0
    eval_payload: dict[str, Any] = field(default_factory=dict)


def _eval_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _HANDLER_ONLY_PAYLOAD_KEYS}


def run(conn: sqlite3.Connection, job: Row) -> ReviewOutcome:
    """The heavy, side-effect-free-on-the-database half.

    Reads the database (and may call Claude) but writes nothing — every
    insert and state transition happens in `persist()`, inside the caller's
    transaction, matching `implement.py`'s `run`/`persist` split for the same
    reason: a multi-minute A3 call must never hold SQLite's write lock.
    """
    experiment_id = job["experiment_id"]
    if experiment_id is None:
        raise ValueError("REVIEW job has no experiment_id")

    experiments = ExperimentRepository(conn)
    experiment = experiments.get(experiment_id)
    if experiment is None:
        raise ValueError(f"no experiment {experiment_id}")

    strategy_id = experiment["strategy_id"]
    strategies = StrategyRepository(conn)
    strategy = strategies.get(strategy_id)
    if strategy is None:
        raise ValueError(f"no strategy {strategy_id}")

    payload = job["payload"] or {}
    evaluations = EvaluationRepository(conn)
    evaluation_id = payload.get("evaluation_id")
    evaluation = (
        evaluations.get(evaluation_id) if evaluation_id is not None else evaluations.latest_for_experiment(experiment_id)
    )
    if evaluation is None:
        raise ValueError(f"REVIEW job has no evaluation to review for experiment {experiment_id}")

    # Guard: A3 is invoked only below the bar (App-Flow §6.1). A stale REVIEW
    # racing a bar-clear (this evaluation actually passed, or the strategy
    # already cleared the bar through some other experiment) is a no-op.
    already_cleared = strategy.get("best_experiment_id") is not None or strategy["status"] == "pending_promotion"
    if evaluation.get("bar_result") == "pass" or already_cleared:
        return ReviewOutcome(
            stale=True,
            strategy_id=strategy_id,
            reviewed_experiment_id=experiment_id,
            evaluation_id=evaluation["id"],
            eval_payload=_eval_payload(payload),
        )

    bar = AcceptanceBar.locked(conn, payload.get("campaign"))

    forced_reason: str | None = None
    if strategy["plateau_counter"] >= bar.plateau_patience:
        forced_reason = (
            f"{strategy['plateau_counter']} consecutive bar failures reached "
            f"plateau_patience ({bar.plateau_patience})"
        )
    elif strategy["iteration_count"] >= bar.hard_iteration_cap:
        forced_reason = f"iteration_count {strategy['iteration_count']} reached hard_iteration_cap ({bar.hard_iteration_cap})"
    else:
        tokens = budget_check(conn, "strategy", "tokens", "day", scope_id=strategy_id)
        iterations = budget_check(conn, "strategy", "iterations", "lifetime", scope_id=strategy_id)
        if not tokens.allowed:
            forced_reason = tokens.reason
        elif not iterations.allowed:
            forced_reason = iterations.reason

    if forced_reason is not None:
        return ReviewOutcome(
            stale=False,
            strategy_id=strategy_id,
            reviewed_experiment_id=experiment_id,
            evaluation_id=evaluation["id"],
            verdict="plateau",
            diagnosis=f"Stopped before invoking A3: {forced_reason}",
            prompt_version=None,
            tokens_spent=0,
            eval_payload=_eval_payload(payload),
        )

    spec = SpecRepository(conn).load_spec(experiment["spec_id"])
    experiment_history = experiments.find(strategy_id=strategy_id, order_by="iteration")
    diagnostic_checks = EvaluationTestRepository(conn).for_evaluation(evaluation["id"])
    regime_rows = RegimePerformanceRepository(conn).for_evaluation(evaluation["id"])
    knowledge_entries = KnowledgeEntryRepository(conn).relevant_to(strategy)

    brief = assemble_review_brief(
        strategy=strategy,
        spec=spec,
        experiment_history=experiment_history,
        evaluation=evaluation,
        diagnostic_checks=diagnostic_checks,
        regime_performance=regime_rows,
        knowledge_entries=knowledge_entries,
        iteration_count=strategy["iteration_count"],
        plateau_counter=strategy["plateau_counter"],
        hard_iteration_cap=bar.hard_iteration_cap,
        plateau_patience=bar.plateau_patience,
    )
    prompt_version = review_prompt_version()
    response = _get_session().review(brief, prompt_version=prompt_version)
    plan = response.plan

    return ReviewOutcome(
        stale=False,
        strategy_id=strategy_id,
        reviewed_experiment_id=experiment_id,
        evaluation_id=evaluation["id"],
        verdict=plan.verdict,
        diagnosis=plan.diagnosis,
        evidence_cited=plan.evidence_cited,
        proposed_changes=plan.proposed_changes,
        expected_effect=plan.expected_effect,
        confidence=plan.confidence,
        prompt_version=prompt_version,
        tokens_spent=response.tokens_spent,
        eval_payload=_eval_payload(payload),
    )


def persist(conn: sqlite3.Connection, job: Row, outcome: ReviewOutcome) -> HandlerResult:
    """The short, atomic half: every database write, in one transaction."""
    if outcome.stale:
        return HandlerResult(tokens_spent=0)

    plans = ResearchPlanRepository(conn)
    plan_id = plans.insert(
        experiment_id=outcome.reviewed_experiment_id,
        strategy_id=outcome.strategy_id,
        verdict=outcome.verdict,
        diagnosis=outcome.diagnosis,
        evidence_cited=outcome.evidence_cited,
        proposed_changes=outcome.proposed_changes,
        expected_effect=outcome.expected_effect,
        confidence=outcome.confidence,
        prompt_version=outcome.prompt_version,
    )

    experiments = ExperimentRepository(conn)
    strategies = StrategyRepository(conn)
    strategy = strategies.get(outcome.strategy_id)

    transition(conn, "experiments", outcome.reviewed_experiment_id, "reviewed", actor="agent:A3")
    if strategy["status"] == "evaluating":
        transition(conn, "strategies", outcome.strategy_id, "evaluated", actor="agent:A3")
        strategy = strategies.get(outcome.strategy_id)

    if outcome.verdict == "iterate":
        transition(conn, "strategies", outcome.strategy_id, "iterating", actor="agent:A3")
        budget_consume(conn, "strategy", "iterations", "lifetime", 1, scope_id=outcome.strategy_id)
        emit(
            conn,
            Event.REVIEW_ITERATE,
            strategy_id=outcome.strategy_id,
            experiment_id=outcome.reviewed_experiment_id,
            payload={**outcome.eval_payload, "research_plan_id": plan_id},
        )
    else:
        if outcome.verdict == "plateau":
            experiments.update(
                outcome.reviewed_experiment_id, outcome="plateaued", failure_reason="plateaued_below_bar"
            )
            to_status = "plateaued"
        else:  # "reject" — the evaluation's own outcome/failure_reason stand as-is
            to_status = "rejected"
        transition(
            conn, "strategies", outcome.strategy_id, to_status, actor="agent:A3", reasoning=outcome.diagnosis
        )
        emit(
            conn,
            Event.REVIEW_PLATEAU_OR_REJECT,
            strategy_id=outcome.strategy_id,
            experiment_id=outcome.reviewed_experiment_id,
            payload={**outcome.eval_payload, "research_plan_id": plan_id},
        )

    budget_consume(conn, "strategy", "tokens", "day", outcome.tokens_spent, scope_id=outcome.strategy_id)
    return HandlerResult(tokens_spent=outcome.tokens_spent)

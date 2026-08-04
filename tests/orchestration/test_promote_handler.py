"""The `PROMOTE` handler (Stage 8 — Implementation_Plan §11): A4's three
decisions and their state transitions, and the "never yes alone" guardrail.
"""
from __future__ import annotations

import pytest

from aqrl.agents.session import ProposedPromotion, StubPromotionSession
from aqrl.db import transaction
from aqrl.db.repositories import (
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    JobRepository,
    PromotionRepository,
    ResearchGoalRepository,
    StrategyRepository,
)
from aqrl.orchestration.budgets import BudgetRepository
from aqrl.orchestration.handlers import promote as handler


@pytest.fixture(autouse=True)
def _reset_session():
    handler.set_session(None)
    yield
    handler.set_session(None)


@pytest.fixture
def experiment_id(conn, strategy_id: int, spec_id: int, snapshot_id: int) -> int:
    experiments = ExperimentRepository(conn)
    iteration = experiments.next_iteration(strategy_id)
    return experiments.start(
        strategy_id, iteration, spec_id=spec_id, data_snapshot_id=snapshot_id, code_commit="deadbeef"
    )


def _clear_the_bar(conn, strategy_id: int, experiment_id: int) -> None:
    """Simulates what `handlers/evaluate.py` already did before enqueuing
    `PROMOTE`: a bar-clearing evaluation, a capacity check row, the
    experiment closed out, and the strategy moved to `pending_promotion` —
    exactly the state a real `PROMOTE` job would find."""
    evaluations = EvaluationRepository(conn)
    evaluation_id = evaluations.insert(
        experiment_id=experiment_id,
        phase="bar",
        result="pass",
        bar_result="pass",
        honest_score=0.9,
        sharpe=1.2,
        n_trials_used=3,
    )
    EvaluationTestRepository(conn).insert(
        evaluation_id=evaluation_id,
        test_name="equities_capacity",
        category="performance",
        result="pass",
        gating=False,
        value=0.01,
        threshold=0.1,
    )
    ExperimentRepository(conn).complete(experiment_id, status="evaluated", outcome="passed", phase_reached="bar")
    StrategyRepository(conn).record_bar_clear(strategy_id, experiment_id, 0.9)


def _job(strategy_id: int, experiment_id: int) -> dict:
    return {"job_type": "PROMOTE", "strategy_id": strategy_id, "experiment_id": experiment_id, "payload": {}}


def _promotion(decision: str, **overrides) -> ProposedPromotion:
    fields = dict(
        decision=decision,
        rationale="fixture rationale",
        overfitting_risk="low",
        capacity_liquidity_ok=True,
        confidence=0.7,
    )
    fields.update(overrides)
    return ProposedPromotion(**fields)


# -- run(): validation ----------------------------------------------------------


def test_run_requires_strategy_id():
    with pytest.raises(ValueError, match="strategy_id"):
        handler.run(None, {"job_type": "PROMOTE", "strategy_id": None, "payload": {}})


def test_run_requires_best_experiment_id(conn, strategy_id):
    with pytest.raises(ValueError, match="best_experiment_id"):
        handler.run(conn, _job(strategy_id, 0))


# -- guards -----------------------------------------------------------------------


def test_a_strategy_not_pending_promotion_is_a_stale_no_op(conn, strategy_id, experiment_id):
    """No session installed at all — if a stale job ever tried to call one,
    this would crash on the lazy `AnthropicSession` import."""
    _clear_the_bar(conn, strategy_id, experiment_id)
    StrategyRepository(conn).update(strategy_id, status="awaiting_human_review")

    outcome = handler.run(conn, _job(strategy_id, experiment_id))
    assert outcome.stale is True

    with transaction(conn, immediate=True):
        result = handler.persist(conn, {}, outcome)
    assert result.tokens_spent == 0
    assert PromotionRepository(conn).find(strategy_id=strategy_id) == []
    assert JobRepository(conn).find(job_type="ARCHIVE") == []


def test_a_strategy_already_decided_is_a_stale_no_op(conn, strategy_id, experiment_id):
    """A race: two PROMOTE jobs for the same strategy (should not happen on
    the current wiring, but must not double-decide if it ever does)."""
    _clear_the_bar(conn, strategy_id, experiment_id)
    PromotionRepository(conn).insert(
        strategy_id=strategy_id,
        best_experiment_id=experiment_id,
        stage_from="research",
        stage_to="human_review",
        decision="approve",
        rationale="already decided",
        overfitting_risk="low",
        capacity_liquidity_ok=True,
        requires_human_approval=True,
        human_decision="pending",
    )

    outcome = handler.run(conn, _job(strategy_id, experiment_id))
    assert outcome.stale is True


# -- approve: the only path toward human review ------------------------------


def test_approve_transitions_and_always_requires_human_approval(conn, strategy_id, experiment_id):
    _clear_the_bar(conn, strategy_id, experiment_id)
    handler.set_session(StubPromotionSession(_promotion("approve")))

    job = _job(strategy_id, experiment_id)
    outcome = handler.run(conn, job)
    assert outcome.stale is False
    assert outcome.decision == "approve"

    with transaction(conn, immediate=True):
        result = handler.persist(conn, job, outcome)
    assert result.tokens_spent == 0

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "awaiting_human_review"

    promotion = PromotionRepository(conn).latest_for_strategy(strategy_id)
    assert promotion["decision"] == "approve"
    # Never self-certifying — App-Flow §7.3.
    assert bool(promotion["requires_human_approval"]) is True
    assert promotion["human_decision"] == "pending"
    assert promotion["iterations_considered"] == strategy["iteration_count"]

    archive_jobs = JobRepository(conn).find(job_type="ARCHIVE")
    assert len(archive_jobs) == 1
    assert archive_jobs[0]["strategy_id"] == strategy_id
    assert archive_jobs[0]["experiment_id"] == experiment_id


# -- reject: A4's own authority, no human needed ------------------------------


def test_reject_transitions_to_rejected_and_still_requires_human_approval_flag(conn, strategy_id, experiment_id):
    _clear_the_bar(conn, strategy_id, experiment_id)
    handler.set_session(StubPromotionSession(_promotion("reject", overfitting_risk="high")))

    job = _job(strategy_id, experiment_id)
    outcome = handler.run(conn, job)
    with transaction(conn, immediate=True):
        handler.persist(conn, job, outcome)

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "rejected"

    promotion = PromotionRepository(conn).latest_for_strategy(strategy_id)
    assert promotion["decision"] == "reject"
    assert promotion["overfitting_risk"] == "high"

    assert JobRepository(conn).find(job_type="ARCHIVE")[0]["strategy_id"] == strategy_id


# -- defer: status untouched, a fresh goal reopens research -------------------


def test_defer_leaves_status_untouched_and_creates_a_research_goal(conn, strategy_id, experiment_id):
    _clear_the_bar(conn, strategy_id, experiment_id)
    handler.set_session(StubPromotionSession(_promotion("defer", rationale="promising but needs more evidence")))

    job = _job(strategy_id, experiment_id)
    outcome = handler.run(conn, job)
    with transaction(conn, immediate=True):
        handler.persist(conn, job, outcome)

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "pending_promotion"  # untouched — PRD §9.2 already stopped this strategy

    goals = ResearchGoalRepository(conn).find(status="active")
    assert len(goals) == 1
    assert goals[0]["market"] == strategy["market"]
    assert goals[0]["timeframe"] == strategy["timeframe"]
    assert goals[0]["description"] == "promising but needs more evidence"

    assert JobRepository(conn).find(job_type="ARCHIVE")[0]["strategy_id"] == strategy_id


# -- budgets: consumed like every other agent call ----------------------------


def test_approve_consumes_strategy_scoped_token_budget(conn, strategy_id, experiment_id):
    _clear_the_bar(conn, strategy_id, experiment_id)
    BudgetRepository(conn).upsert("strategy", "tokens", "day", 1000, scope_id=strategy_id)

    class _CountingSession(StubPromotionSession):
        def decide(self, brief, *, prompt_version):
            response = super().decide(brief, prompt_version=prompt_version)
            return type(response)(
                promotion=response.promotion,
                prompt_version=response.prompt_version,
                tokens_spent=75,
                raw_output=response.raw_output,
            )

    handler.set_session(_CountingSession(_promotion("approve")))
    job = _job(strategy_id, experiment_id)
    outcome = handler.run(conn, job)
    with transaction(conn, immediate=True):
        handler.persist(conn, job, outcome)

    tokens_budget = BudgetRepository(conn).for_scope("strategy", "tokens", "day", strategy_id)
    assert tokens_budget["used_value"] == 75

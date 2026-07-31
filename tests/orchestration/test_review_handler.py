"""The `REVIEW` handler (Stage 6 — Implementation_Plan §9): A3's guards,
stop conditions, and the three verdicts' transitions/events.
"""
from __future__ import annotations

import pytest

from aqrl.agents.session import ProposedPlan, StubReviewSession
from aqrl.db import transaction
from aqrl.db.repositories import (
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    JobRepository,
    RegimePerformanceRepository,
    ResearchPlanRepository,
    StrategyRepository,
)
from aqrl.orchestration.budgets import BudgetRepository
from aqrl.orchestration.handlers import review as handler

from .conftest import ASSET_CLASS


@pytest.fixture(autouse=True)
def _reset_session():
    """Every test starts with the default (lazy `AnthropicSession`) unless it
    explicitly installs a stub — no test may leak a session into another."""
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


def _fail_the_bar(conn, strategy_id: int, experiment_id: int, *, bar_failed_on: str = "min_trades") -> int:
    """Simulates what `handlers/evaluate.py` already did before enqueuing
    `REVIEW`: an `evaluations` row recording a bar failure, plus the
    `evaluation_tests`/`regime_performance` rows a real report would carry,
    the strategy's plateau counter bumped, and the experiment closed out —
    exactly the state a real `REVIEW` job would find."""
    evaluations = EvaluationRepository(conn)
    evaluation_id = evaluations.insert(
        experiment_id=experiment_id, phase="bar", result="fail", bar_result="fail", bar_failed_on=bar_failed_on
    )
    EvaluationTestRepository(conn).insert(
        evaluation_id=evaluation_id,
        test_name=bar_failed_on,
        category="performance",
        result="fail",
        gating=True,
        value=42.0,
        threshold=100.0,
    )
    RegimePerformanceRepository(conn).insert(
        evaluation_id=evaluation_id, regime="trending", sharpe=0.1, cagr=0.02, max_drawdown=0.1, trade_count=20
    )
    ExperimentRepository(conn).complete(
        experiment_id, status="evaluated", outcome="failed", phase_reached="bar", failure_reason="insufficient_trades"
    )
    StrategyRepository(conn).record_bar_failure(strategy_id)
    return evaluation_id


def _job(experiment_id: int, evaluation_id: int, **payload_overrides) -> dict:
    payload = {"asset_class": ASSET_CLASS, "evaluation_id": evaluation_id, **payload_overrides}
    return {"job_type": "REVIEW", "experiment_id": experiment_id, "payload": payload}


def _iterate_plan(diagnosis: str = "widen the entry gap") -> ProposedPlan:
    return ProposedPlan(
        verdict="iterate",
        diagnosis=diagnosis,
        evidence_cited=[{"note": "test fixture"}],
        proposed_changes=[{"target": "entry", "change": "widen the gap", "reason": "reduce whipsaw"}],
        expected_effect="fewer false crossovers",
        confidence=0.4,
    )


# -- run(): validation --------------------------------------------------------


def test_run_requires_experiment_id():
    with pytest.raises(ValueError, match="experiment_id"):
        handler.run(None, {"job_type": "REVIEW", "experiment_id": None, "payload": {}})


def test_run_requires_an_evaluation_to_review(conn, strategy_id, experiment_id):
    job = {"job_type": "REVIEW", "experiment_id": experiment_id, "payload": {"asset_class": ASSET_CLASS}}
    with pytest.raises(ValueError, match="no evaluation"):
        handler.run(conn, job)


# -- guards: A3 is only ever invoked below the bar (App-Flow §6.1) ------------


def test_a_passing_evaluation_is_a_stale_no_op(conn, strategy_id, experiment_id):
    """A3 must never be consulted about an evaluation that actually passed —
    if this ever reached a session with none installed, it would crash on
    the lazy `AnthropicSession` import rather than silently succeeding."""
    evaluation_id = EvaluationRepository(conn).insert(
        experiment_id=experiment_id, phase="P3", result="pass", bar_result="pass"
    )
    outcome = handler.run(conn, _job(experiment_id, evaluation_id))
    assert outcome.stale is True

    with transaction(conn, immediate=True):
        result = handler.persist(conn, {}, outcome)
    assert result.tokens_spent == 0
    assert ResearchPlanRepository(conn).find(experiment_id=experiment_id) == []
    assert JobRepository(conn).find(job_type="IMPLEMENT") == []
    assert JobRepository(conn).find(job_type="ARCHIVE") == []


def test_a_strategy_that_already_cleared_the_bar_is_a_stale_no_op(conn, strategy_id, experiment_id):
    """A race: some other experiment already cleared the bar for this
    strategy. This REVIEW job is for a failure that no longer matters."""
    evaluation_id = _fail_the_bar(conn, strategy_id, experiment_id)
    StrategyRepository(conn).record_bar_clear(strategy_id, experiment_id, 0.9)

    outcome = handler.run(conn, _job(experiment_id, evaluation_id))
    assert outcome.stale is True


# -- stop conditions, all checked before invoking Claude ----------------------


def test_plateau_patience_forces_plateau_with_zero_tokens(conn, strategy_id, experiment_id):
    evaluation_id = _fail_the_bar(conn, strategy_id, experiment_id)
    # `_fail_the_bar` already bumped the counter once; four more reaches the
    # default `plateau_patience` of 5.
    for _ in range(4):
        StrategyRepository(conn).record_bar_failure(strategy_id)

    outcome = handler.run(conn, _job(experiment_id, evaluation_id))
    assert outcome.stale is False
    assert outcome.verdict == "plateau"
    assert outcome.tokens_spent == 0
    assert "plateau_patience" in outcome.diagnosis


def test_hard_iteration_cap_forces_plateau(conn, strategy_id, experiment_id):
    evaluation_id = _fail_the_bar(conn, strategy_id, experiment_id)
    StrategyRepository(conn).update(strategy_id, iteration_count=25)

    outcome = handler.run(conn, _job(experiment_id, evaluation_id))
    assert outcome.verdict == "plateau"
    assert outcome.tokens_spent == 0
    assert "hard_iteration_cap" in outcome.diagnosis


def test_exhausted_strategy_token_budget_forces_plateau(conn, strategy_id, experiment_id):
    evaluation_id = _fail_the_bar(conn, strategy_id, experiment_id)
    BudgetRepository(conn).upsert("strategy", "tokens", "day", 100, scope_id=strategy_id)
    conn.execute(
        "UPDATE budgets SET used_value = 100, exhausted = 1 WHERE scope = 'strategy' AND scope_id = ?",
        (strategy_id,),
    )

    outcome = handler.run(conn, _job(experiment_id, evaluation_id))
    assert outcome.verdict == "plateau"
    assert outcome.tokens_spent == 0
    assert "budget_exhausted" in outcome.diagnosis


def test_stop_conditions_never_touch_a_session(conn, strategy_id, experiment_id):
    """No `ReviewSession` installed at all — if a forced stop ever tried to
    call one, this would crash on the lazy `AnthropicSession` import."""
    evaluation_id = _fail_the_bar(conn, strategy_id, experiment_id)
    StrategyRepository(conn).update(strategy_id, iteration_count=25)
    outcome = handler.run(conn, _job(experiment_id, evaluation_id))
    assert outcome.verdict == "plateau"


# -- the iterate verdict: loops back to A2 ------------------------------------


def test_iterate_verdict_transitions_and_emits_implement(conn, strategy_id, experiment_id):
    evaluation_id = _fail_the_bar(conn, strategy_id, experiment_id)
    handler.set_session(StubReviewSession(_iterate_plan()))

    job = _job(experiment_id, evaluation_id, data_snapshot_id=99, cost_multiplier=3.0)
    outcome = handler.run(conn, job)
    assert outcome.stale is False
    assert outcome.verdict == "iterate"

    with transaction(conn, immediate=True):
        result = handler.persist(conn, job, outcome)
    assert result.tokens_spent == 0

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "iterating"

    experiment = ExperimentRepository(conn).get(experiment_id)
    assert experiment["status"] == "reviewed"

    plans = ResearchPlanRepository(conn).find(experiment_id=experiment_id)
    assert len(plans) == 1
    assert plans[0]["verdict"] == "iterate"

    implement_jobs = JobRepository(conn).find(job_type="IMPLEMENT")
    assert len(implement_jobs) == 1
    payload = implement_jobs[0]["payload"]
    assert payload["research_plan_id"] == plans[0]["id"]
    # The eval-relevant payload carried through unchanged; `evaluation_id` stripped.
    assert payload["asset_class"] == ASSET_CLASS
    assert payload["data_snapshot_id"] == 99
    assert payload["cost_multiplier"] == 3.0
    assert "evaluation_id" not in payload


# -- the plateau verdict: routes to A5, never A4 ------------------------------


def test_plateau_verdict_closes_the_strategy_and_marks_the_experiment(conn, strategy_id, experiment_id):
    evaluation_id = _fail_the_bar(conn, strategy_id, experiment_id)
    handler.set_session(
        StubReviewSession(ProposedPlan(verdict="plateau", diagnosis="no progress across attempts", confidence=0.2))
    )

    job = _job(experiment_id, evaluation_id)
    outcome = handler.run(conn, job)
    with transaction(conn, immediate=True):
        handler.persist(conn, job, outcome)

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "plateaued"

    experiment = ExperimentRepository(conn).get(experiment_id)
    assert experiment["status"] == "reviewed"
    assert experiment["outcome"] == "plateaued"
    assert experiment["failure_reason"] == "plateaued_below_bar"

    assert JobRepository(conn).find(job_type="IMPLEMENT") == []
    archive_jobs = JobRepository(conn).find(job_type="ARCHIVE")
    assert len(archive_jobs) == 1
    assert archive_jobs[0]["strategy_id"] == strategy_id


# -- the reject verdict: routes to A5, never A4, leaves the experiment as-is --


def test_reject_verdict_closes_the_strategy_without_touching_experiment_outcome(conn, strategy_id, experiment_id):
    evaluation_id = _fail_the_bar(conn, strategy_id, experiment_id)
    handler.set_session(
        StubReviewSession(ProposedPlan(verdict="reject", diagnosis="hypothesis unsound", confidence=0.1))
    )

    job = _job(experiment_id, evaluation_id)
    outcome = handler.run(conn, job)
    with transaction(conn, immediate=True):
        handler.persist(conn, job, outcome)

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "rejected"

    experiment = ExperimentRepository(conn).get(experiment_id)
    # Untouched — whatever `evaluate.py`'s own persist already recorded.
    assert experiment["outcome"] == "failed"
    assert experiment["failure_reason"] == "insufficient_trades"

    assert JobRepository(conn).find(job_type="ARCHIVE")[0]["strategy_id"] == strategy_id


# -- budgets: Stage 6 is the first real consumer of per-strategy budgets -----


def test_iterate_consumes_strategy_scoped_budgets(conn, strategy_id, experiment_id):
    evaluation_id = _fail_the_bar(conn, strategy_id, experiment_id)
    BudgetRepository(conn).upsert("strategy", "iterations", "lifetime", 10, scope_id=strategy_id)

    class _CountingSession(StubReviewSession):
        def review(self, brief, *, prompt_version):
            response = super().review(brief, prompt_version=prompt_version)
            return type(response)(
                plan=response.plan, prompt_version=response.prompt_version, tokens_spent=50, raw_output=response.raw_output
            )

    handler.set_session(_CountingSession(_iterate_plan()))
    job = _job(experiment_id, evaluation_id)
    outcome = handler.run(conn, job)
    with transaction(conn, immediate=True):
        handler.persist(conn, job, outcome)

    iterations_budget = BudgetRepository(conn).for_scope("strategy", "iterations", "lifetime", strategy_id)
    assert iterations_budget["used_value"] == 1

    tokens_budget = BudgetRepository(conn).for_scope("strategy", "tokens", "day", strategy_id)
    # No row was ever configured for tokens, so `consume` is a no-op — it must
    # not create one just to track spend nobody capped (`budgets.py`).
    assert tokens_budget is None

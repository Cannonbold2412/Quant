"""The `GENERATE_SPEC` handler (Stage 7 — Implementation_Plan §10): A1's
budget guard, exact-duplicate rejection, structural near-duplicate note, and
the novel-spec -> `SPEC_SAVED` -> `IMPLEMENT` handoff.
"""
from __future__ import annotations

import pytest

from aqrl.agents.embeddings import StubEmbedder
from aqrl.agents.session import ProposedHypothesis, StubHypothesisSession
from aqrl.db import transaction
from aqrl.db.repositories import (
    AuditLogRepository,
    JobRepository,
    ResearchGoalRepository,
    SpecRepository,
    StrategyRepository,
)
from aqrl.operators.spec import Node
from aqrl.orchestration.handlers import generate as handler

from .conftest import ASSET_CLASS, MARKET, TIMEFRAME


@pytest.fixture(autouse=True)
def _reset_handler_state():
    """No test may leak a session or embedder into another — same
    reasoning as `implement.py`'s/`review.py`'s own `_reset_session`."""
    handler.set_session(None)
    handler.set_embedder(None)
    yield
    handler.set_session(None)
    handler.set_embedder(None)


@pytest.fixture(autouse=True)
def _stub_embedder():
    handler.set_embedder(StubEmbedder())


@pytest.fixture
def goal_id(conn) -> int:
    return ResearchGoalRepository(conn).insert(
        title="Find swing alpha",
        description="NSE equities, daily bars",
        market=MARKET,
        timeframe=TIMEFRAME,
        allocation_bucket="incremental",
        priority=0,
        hypothesis_budget=5,
        hypotheses_used=0,
        status="active",
        created_by="human",
    )


def _job(goal_id: int, **payload_overrides) -> dict:
    payload = {"goal_id": goal_id, "asset_class": ASSET_CLASS, **payload_overrides}
    return {"job_type": "GENERATE_SPEC", "strategy_id": None, "experiment_id": None, "payload": payload}


def _hypothesis(
    *, name: str = "ema-cross-1", family: str = "gen-fixture-family", fast: int = 5, slow: int = 20
) -> ProposedHypothesis:
    return ProposedHypothesis(
        name=name,
        family=family,
        market=MARKET,
        timeframe=TIMEFRAME,
        entry_logic=[
            Node(id="fast", operator="ema", inputs={"series": "price.close"}, params={"span": fast}),
            Node(id="slow", operator="ema", inputs={"series": "price.close"}, params={"span": slow}),
            Node(id="e1", operator="crossover", inputs={"fast": "fast", "slow": "slow"}),
        ],
        hypothesis="ema crossover captures swing moves",
        rationale="no contradicting internal lessons surfaced",
    )


# -- run(): budget / status guards --------------------------------------------


def test_run_raises_without_goal_id(conn):
    with pytest.raises(ValueError, match="goal_id"):
        handler.run(conn, {"job_type": "GENERATE_SPEC", "payload": {}})


def test_run_raises_for_unknown_goal(conn):
    with pytest.raises(ValueError, match="no research_goals row"):
        handler.run(conn, _job(999_999))


def test_forced_stop_when_goal_not_active(conn, goal_id):
    with transaction(conn, immediate=True):
        conn.execute("UPDATE research_goals SET status = 'paused' WHERE id = ?", (goal_id,))

    outcome = handler.run(conn, _job(goal_id))

    assert outcome.forced_stop is True
    assert "paused" in outcome.reason
    assert outcome.tokens_spent == 0


def test_forced_stop_when_budget_exhausted(conn, goal_id):
    with transaction(conn, immediate=True):
        conn.execute(
            "UPDATE research_goals SET hypothesis_budget = 1, hypotheses_used = 1 WHERE id = ?", (goal_id,)
        )

    outcome = handler.run(conn, _job(goal_id))

    assert outcome.forced_stop is True
    assert "hypothesis_budget" in outcome.reason


def test_no_budget_row_means_unlimited(conn, goal_id):
    """`hypothesis_budget IS NULL` is unlimited, same "no row/no cap"
    convention `budgets.py` uses — never a forced stop."""
    with transaction(conn, immediate=True):
        conn.execute("UPDATE research_goals SET hypothesis_budget = NULL WHERE id = ?", (goal_id,))
    handler.set_session(StubHypothesisSession(_hypothesis()))

    outcome = handler.run(conn, _job(goal_id))

    assert outcome.forced_stop is False


def test_persist_forced_stop_does_not_touch_budget_or_strategies(conn, goal_id):
    with transaction(conn, immediate=True):
        conn.execute("UPDATE research_goals SET status = 'paused' WHERE id = ?", (goal_id,))
    outcome = handler.run(conn, _job(goal_id))

    with transaction(conn, immediate=True):
        result = handler.persist(conn, _job(goal_id), outcome)

    assert result.tokens_spent == 0
    goal = ResearchGoalRepository(conn).get(goal_id)
    assert goal["hypotheses_used"] == 0
    assert StrategyRepository(conn).find() == []
    actions = [row["action"] for row in AuditLogRepository(conn).find()]
    assert "generate_spec.forced_stop" in actions


# -- run()/persist(): a novel hypothesis --------------------------------------


def test_novel_hypothesis_creates_strategy_and_enqueues_implement(conn, goal_id):
    handler.set_session(StubHypothesisSession(_hypothesis()))

    outcome = handler.run(conn, _job(goal_id))
    assert outcome.forced_stop is False
    assert outcome.exact_duplicate is False

    with transaction(conn, immediate=True):
        handler.persist(conn, _job(goal_id), outcome)

    strategies = StrategyRepository(conn).find()
    assert len(strategies) == 1
    assert strategies[0]["name"] == "ema-cross-1"
    assert strategies[0]["status"] == "spec_ready"

    specs = SpecRepository(conn).find()
    assert len(specs) == 1
    assert specs[0]["source_internal_knowledge_ids"] == []

    implement_jobs = JobRepository(conn).find(job_type="IMPLEMENT")
    assert len(implement_jobs) == 1
    assert implement_jobs[0]["payload"]["asset_class"] == ASSET_CLASS
    assert implement_jobs[0]["payload"]["spec_id"] == specs[0]["id"]

    goal = ResearchGoalRepository(conn).get(goal_id)
    assert goal["hypotheses_used"] == 1


def test_asset_class_falls_back_to_markets_first_declared_class(conn, goal_id):
    """When the enqueuer didn't supply `asset_class` (nothing on
    `research_goals` carries one), the handler falls back to the market
    profile's first declared asset class rather than raising."""
    handler.set_session(StubHypothesisSession(_hypothesis()))
    job = _job(goal_id)
    del job["payload"]["asset_class"]

    outcome = handler.run(conn, job)
    assert outcome.eval_payload["asset_class"]

    with transaction(conn, immediate=True):
        handler.persist(conn, job, outcome)

    implement_jobs = JobRepository(conn).find(job_type="IMPLEMENT")
    assert implement_jobs[0]["payload"]["asset_class"] == outcome.eval_payload["asset_class"]


# -- run()/persist(): exact duplicate -----------------------------------------


def test_exact_duplicate_is_rejected_before_persist(conn, goal_id):
    handler.set_session(StubHypothesisSession(_hypothesis()))
    first = handler.run(conn, _job(goal_id))
    with transaction(conn, immediate=True):
        handler.persist(conn, _job(goal_id), first)

    # Same operator DAG, same params -> identical spec_hash.
    second = handler.run(conn, _job(goal_id))
    assert second.exact_duplicate is True
    assert second.spec_hash == first.spec_hash

    with transaction(conn, immediate=True):
        handler.persist(conn, _job(goal_id), second)

    # No second strategy, no second IMPLEMENT job — but the budget still
    # counted the attempt (App-Flow §3.4: reject before *further* compute).
    assert len(StrategyRepository(conn).find()) == 1
    assert len(JobRepository(conn).find(job_type="IMPLEMENT")) == 1
    goal = ResearchGoalRepository(conn).get(goal_id)
    assert goal["hypotheses_used"] == 2
    actions = [row["action"] for row in AuditLogRepository(conn).find()]
    assert "generate_spec.exact_duplicate" in actions


# -- run()/persist(): structural near-duplicate -------------------------------


def test_near_duplicate_proceeds_as_novel_but_is_logged(conn, goal_id):
    """Same operator set, same family, different parameters — App-Flow
    §3.4's near-duplicate outcome: it *is* structurally different, so it
    proceeds, but the connection is recorded on the audit trail."""
    handler.set_session(StubHypothesisSession(_hypothesis(name="strat-a", fast=5, slow=20)))
    first = handler.run(conn, _job(goal_id))
    with transaction(conn, immediate=True):
        handler.persist(conn, _job(goal_id), first)

    handler.set_session(StubHypothesisSession(_hypothesis(name="strat-b", fast=6, slow=21)))
    second = handler.run(conn, _job(goal_id))
    assert second.exact_duplicate is False
    assert second.near_duplicates, "same operator set, different params, should be flagged near-duplicate"

    with transaction(conn, immediate=True):
        handler.persist(conn, _job(goal_id), second)

    assert len(StrategyRepository(conn).find()) == 2
    actions = [row["action"] for row in AuditLogRepository(conn).find()]
    assert "generate_spec.near_duplicate" in actions

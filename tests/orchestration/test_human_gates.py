"""Stage 9 — Human Gates (Implementation_Plan §12): `aqrl.gates.approve`/
`reject`, the only code paths in the system that merge a strategy branch,
open the vault, or write a `deployments` row (TRD §18: no code path may
bypass these).

Reuses `test_promote_handler.py`'s `_clear_the_bar`/`_job`/`_promotion`
helpers to reach the exact `promotions` row state a real `PROMOTE` job would
leave — Stage 9 is the next stage in the same pipeline, not a fresh fixture
universe.
"""
from __future__ import annotations

import pytest

from aqrl import gates
from aqrl.agents.session import StubPromotionSession
from aqrl.db import transaction
from aqrl.db.repositories import (
    DeploymentRepository,
    ExperimentRepository,
    KnowledgeEntryRepository,
    PromotionRepository,
    SpecRepository,
    StrategyRepository,
    VaultAccessRepository,
    repeat_failure_rate,
)
from aqrl.orchestration.handlers import promote as promote_handler
from aqrl.vcs import StrategyRepo

from .conftest import crossover_spec
from .test_promote_handler import _clear_the_bar, _job, _promotion


@pytest.fixture(autouse=True)
def _reset_session():
    promote_handler.set_session(None)
    yield
    promote_handler.set_session(None)


def _approve_via_a4(conn, strategy_id: int, experiment_id: int) -> dict:
    """Runs the real PROMOTE handler to leave the pending `promotions` row
    Stage 9's `aqrl review` is meant to find."""
    _clear_the_bar(conn, strategy_id, experiment_id)
    promote_handler.set_session(StubPromotionSession(_promotion("approve")))
    job = _job(strategy_id, experiment_id)
    outcome = promote_handler.run(conn, job)
    with transaction(conn, immediate=True):
        promote_handler.persist(conn, job, outcome)
    return PromotionRepository(conn).latest_for_strategy(strategy_id)


def _give_branch(conn, settings, strategy_id: int) -> str:
    """`gates.approve` needs `strategies.git_branch` set with real content —
    nothing in this fixture chain runs `IMPLEMENT`, so commit it by hand, the
    same shape `implement.py` would have left."""
    strategy = StrategyRepository(conn).get(strategy_id)
    branch = f"strategy/{strategy['uid']}"
    StrategyRepo(settings.strategy_repo_path).commit_file(
        branch, f"strategies/{strategy['uid']}/strategy.py", "x = 1\n", "iteration 1"
    )
    StrategyRepository(conn).update(strategy_id, git_branch=branch)
    return branch


# -- approve --------------------------------------------------------------------


def test_approve_writes_deployment_merges_and_transitions(conn, settings, strategy_id, experiment_id):
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)
    _give_branch(conn, settings, strategy_id)

    result = gates.approve(conn, promotion["id"], by="kiran", note="clean walk-forward, low overfitting risk")

    assert result["merge_commit"]
    assert result["mode"] == "paper"
    assert result["deploy_branch"] == "deploy/paper"
    assert result["strategy_status"] == "paper_trading"

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "paper_trading"

    updated = PromotionRepository(conn).get(promotion["id"])
    assert updated["human_decision"] == "approved"
    assert updated["human_decided_by"] == "kiran"
    assert updated["merge_commit"] == result["merge_commit"]

    deployments = DeploymentRepository(conn).active_for_strategy(strategy_id)
    assert len(deployments) == 1
    assert deployments[0]["mode"] == "paper"
    assert deployments[0]["deploy_branch"] == "deploy/paper"
    assert deployments[0]["trades_required"]  # daily.yaml's min_trades, whatever it is
    assert deployments[0]["regimes_required"] == ["trending", "sideways", "high_vol", "low_vol"]

    vault_rows = VaultAccessRepository(conn).find(strategy_id=strategy_id)
    assert len(vault_rows) == 1
    assert vault_rows[0]["opened_by"] == "promotion_gate"
    assert vault_rows[0]["budget_before"] == 1
    assert vault_rows[0]["budget_after"] == 0


def test_approve_expected_metrics_come_from_the_winning_evaluation(conn, settings, strategy_id, experiment_id):
    """`_clear_the_bar` records `sharpe=1.2` on the winning evaluation; the
    deployment's baseline must be copied from it, not invented."""
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)
    _give_branch(conn, settings, strategy_id)

    gates.approve(conn, promotion["id"], by="kiran", note="note")

    deployment = DeploymentRepository(conn).active_for_strategy(strategy_id)[0]
    assert deployment["expected_sharpe"] == 1.2


def test_approve_with_empty_note_raises_and_writes_nothing(conn, settings, strategy_id, experiment_id):
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)
    _give_branch(conn, settings, strategy_id)

    with pytest.raises(ValueError, match="typed note"):
        gates.approve(conn, promotion["id"], by="kiran", note="   ")

    assert PromotionRepository(conn).get(promotion["id"])["human_decision"] == "pending"
    assert DeploymentRepository(conn).active_for_strategy(strategy_id) == []


def test_approving_an_already_decided_promotion_raises(conn, settings, strategy_id, experiment_id):
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)
    _give_branch(conn, settings, strategy_id)
    gates.approve(conn, promotion["id"], by="kiran", note="first")

    with pytest.raises(ValueError, match="already"):
        gates.approve(conn, promotion["id"], by="kiran", note="again")


def test_a_second_approval_in_the_same_family_is_blocked_by_the_vault_budget(
    conn, settings, strategy_id, experiment_id, snapshot_id
):
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)
    _give_branch(conn, settings, strategy_id)
    gates.approve(conn, promotion["id"], by="kiran", note="first strategy in the family")

    strategy = StrategyRepository(conn).get(strategy_id)
    strategy_id_2 = StrategyRepository(conn).insert(
        name="orch-fixture-2", family=strategy["family"], market=strategy["market"],
        timeframe=strategy["timeframe"], status="evaluating",
    )
    spec_id_2 = SpecRepository(conn).insert_spec(crossover_spec(fast=5, slow=20), strategy_id_2)
    experiments = ExperimentRepository(conn)
    experiment_id_2 = experiments.start(
        strategy_id_2, experiments.next_iteration(strategy_id_2),
        spec_id=spec_id_2, data_snapshot_id=snapshot_id, code_commit="beadfeed",
    )
    promotion_2 = _approve_via_a4(conn, strategy_id_2, experiment_id_2)
    _give_branch(conn, settings, strategy_id_2)

    vault_count_before = VaultAccessRepository(conn).count(family=strategy["family"])
    with pytest.raises(PermissionError, match="vault budget"):
        gates.approve(conn, promotion_2["id"], by="kiran", note="second strategy, same family")
    assert VaultAccessRepository(conn).count(family=strategy["family"]) == vault_count_before


# -- reject ---------------------------------------------------------------------


def test_reject_writes_knowledge_and_transitions_to_rejected(conn, strategy_id, experiment_id):
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)  # A4 said approve; the human disagrees

    result = gates.reject(
        conn, promotion["id"], by="kiran", note="overfit to one regime",
        reason="overfit_in_sample", next_questions=["does this hold on a longer out-of-sample window?"],
    )

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "rejected"

    updated = PromotionRepository(conn).get(promotion["id"])
    assert updated["human_decision"] == "rejected"
    assert updated["human_notes"] == "overfit to one regime"

    entry = KnowledgeEntryRepository(conn).get(result["knowledge_entry_id"])
    assert entry["evidence"]["failure_reasons"] == ["overfit_in_sample"]
    assert entry["future_ideas"] == ["does this hold on a longer out-of-sample window?"]


def test_reject_feeds_the_repeat_failure_rate_metric(conn, strategy_id, experiment_id, snapshot_id):
    """Implementation_Plan §11's done-when, reached from a human decision
    instead of A5: a later experiment failing identically must count as a
    preventable repeat."""
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)
    gates.reject(
        conn, promotion["id"], by="kiran", note="overfit to one regime",
        reason="overfit_in_sample", next_questions=["retest on a longer window"],
    )

    experiments = ExperimentRepository(conn)
    spec_id_2 = SpecRepository(conn).insert_spec(crossover_spec(fast=3, slow=9), strategy_id)
    later_experiment_id = experiments.start(
        strategy_id, experiments.next_iteration(strategy_id),
        spec_id=spec_id_2, data_snapshot_id=snapshot_id, code_commit="cafefeed",
    )
    experiments.complete(
        later_experiment_id, status="evaluated", outcome="failed",
        phase_reached="P1", failure_reason="overfit_in_sample",
    )

    rate = repeat_failure_rate(conn)
    assert rate["repeats"] == 1


def test_reject_with_unrecognised_reason_raises(conn, strategy_id, experiment_id):
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)
    with pytest.raises(ValueError, match="unrecognised rejection reason"):
        gates.reject(conn, promotion["id"], by="kiran", note="n", reason="bogus_reason", next_questions=["q"])


def test_reject_requires_at_least_one_next_question(conn, strategy_id, experiment_id):
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)
    with pytest.raises(ValueError, match="next-question"):
        gates.reject(conn, promotion["id"], by="kiran", note="n", reason="no_signal", next_questions=[])

"""The `MONITOR_DEPLOYMENT` handler and `aqrl.monitoring`'s human-triggered
actions, end to end — Stage 11 (Implementation_Plan §14).

Needs `vcs.py` (via `gates.approve`/`monitoring.retire`), so this module
collects only where `fcntl` exists (WSL/Linux), the same pre-existing gap
`test_human_gates.py` inherits — see that module's own note.

The scoring math (z-scores, level assignment, the regime-demotion
done-when) is already proven in `tests/test_monitoring.py`, which needs no
database and runs everywhere. This suite's job is different: does the real
`replay()` wire up against a real snapshot, and does `persist()` write the
right rows given a verdict? For the latter, most tests construct a
`MonitorOutcome` directly — the same "stub the nondeterministic part, drive
the real DB write" shape `test_promote_handler.py` uses via `_promotion()`
— rather than fighting operator/backtest numerics for an exact Sharpe.
"""
from __future__ import annotations

import subprocess

import numpy as np
import pytest

from aqrl import gates, monitoring
from aqrl.agents.session import StubPromotionSession
from aqrl.db import transaction
from aqrl.db.repositories import (
    DeploymentRepository,
    ExperimentRepository,
    HealthCheckRepository,
    JobRepository,
    LifecycleEventRepository,
    PromotionRepository,
    ResearchQuestionRepository,
    SpecRepository,
    StrategyRepository,
    TradeRepository,
)
from aqrl.eval.backtest import BacktestResult
from aqrl.eval.tradebook import Trade
from aqrl.orchestration import scheduler
from aqrl.orchestration.handlers import monitor as handler
from aqrl.orchestration.handlers import promote as promote_handler
from aqrl.profiles import ProfileLoader

from .conftest import crossover_spec
from .test_human_gates import _give_branch
from .test_promote_handler import _clear_the_bar, _job, _promotion


@pytest.fixture(autouse=True)
def _reset_session():
    promote_handler.set_session(None)
    yield
    promote_handler.set_session(None)


def _approve_via_a4(conn, strategy_id: int, experiment_id: int) -> dict:
    _clear_the_bar(conn, strategy_id, experiment_id)
    promote_handler.set_session(StubPromotionSession(_promotion("approve")))
    job = _job(strategy_id, experiment_id)
    outcome = promote_handler.run(conn, job)
    with transaction(conn, immediate=True):
        promote_handler.persist(conn, job, outcome)
    return PromotionRepository(conn).latest_for_strategy(strategy_id)


@pytest.fixture
def deployment_id(conn, settings, strategy_id, experiment_id) -> int:
    """A real, active paper deployment via the actual Gate 1 path — no
    stand-in database rows, the same starting point
    `test_human_gates.py` uses."""
    promotion = _approve_via_a4(conn, strategy_id, experiment_id)
    _give_branch(conn, settings, strategy_id)
    approved = gates.approve(conn, promotion["id"], by="kiran", note="clears the bar, reasonable sizing")
    return approved["deployment_id"]


def _monitor_job(deployment_id: int) -> dict:
    return {"job_type": "MONITOR_DEPLOYMENT", "payload": {"deployment_id": deployment_id}}


def _flat_backtest_result(dates: np.ndarray, returns: np.ndarray) -> BacktestResult:
    """A minimal `BacktestResult` — only the fields `compute_metrics`,
    `risk_breach`, and `_slice_result`-shaped construction actually read."""
    n = returns.size
    equity = np.cumprod(1.0 + returns)
    return BacktestResult(
        dates=dates,
        instruments=("AAA",),
        weights=np.ones((n, 1)),
        gross_returns=returns.reshape(-1, 1),
        costs=np.zeros((n, 1)),
        net_returns=returns.reshape(-1, 1),
        portfolio_returns=returns,
        equity=equity,
        cost_multiplier=0.0,
        non_finite_bars=0,
    )


def _make_trade(instrument: str, entry_date: str, exit_date: str, net_return: float) -> Trade:
    return Trade(
        instrument=instrument,
        side=1,
        entry_date=entry_date,
        exit_date=exit_date,
        bars_held=2,
        avg_weight=1.0,
        gross_return=net_return,
        cost=0.0,
        net_return=net_return,
        open_at_end=False,
    )


def _outcome_from_verdict(deployment_id: int, verdict: monitoring.HealthVerdict, *, breach_index=None, gate=None):
    dates = np.array(["2020-01-01", "2020-01-02"], dtype="datetime64[D]")
    # A real profile, not a stand-in: `persist()` reads `resolved.cost_model.
    # slippage_bps` for every trade it actually records.
    resolved = ProfileLoader().resolve("nse_equity", "daily", "cash_equity")
    result = monitoring.ReplayResult(
        resolved=resolved,
        baseline_trades=[],
        paper_trades=[
            _make_trade("AAA", "2020-01-01", "2020-01-01", 0.01),
            _make_trade("AAA", "2020-01-02", "2020-01-02", 0.01),
        ],
        baseline_result=_flat_backtest_result(dates, np.array([0.0, 0.0])),
        paper_result=_flat_backtest_result(dates, np.array([0.01, 0.01])),
        weak_regimes=frozenset(),
        current_regime=None,
        regime_by_date={},
    )
    return handler.MonitorOutcome(
        stale=False,
        deployment_id=deployment_id,
        replay=result,
        verdict=verdict,
        gate=gate or monitoring.GateResult(
            passed=False, trade_count_ok=False, deviation_ok=True,
            regime_coverage_ok=False, execution_ok=True, health_ok=(verdict.level == "green"),
            reasons=("trade count", "regime coverage"),
        ),
        breach_index=breach_index,
    )


def _verdict(level: str) -> monitoring.HealthVerdict:
    return monitoring.HealthVerdict(
        level=level, live_sharpe=1.0, live_max_dd=0.1, live_win_rate=0.6, live_profit_factor=1.5,
        sharpe_zscore=0.0, win_rate_zscore=0.0, avg_trade_zscore=0.0, loss_distribution_pvalue=0.5,
        current_regime=None, regime_historically_weak=False, slippage_deviation=0.0, missed_fill_rate=0.0,
        verdict_reason="fixture", recommended_action={"green": "continue", "red": "stop"}[level],
        trade_count=2, signals_tripped=(),
    )


# -- run(): stale guards --------------------------------------------------------


def test_run_is_stale_for_an_inactive_deployment(conn, deployment_id):
    DeploymentRepository(conn).update(deployment_id, status="stopped")
    outcome = handler.run(conn, _monitor_job(deployment_id))
    assert outcome.stale is True


def test_run_requires_deployment_id_in_payload(conn):
    with pytest.raises(ValueError, match="deployment_id"):
        handler.run(conn, {"job_type": "MONITOR_DEPLOYMENT", "payload": {}})


# -- replay(): real wiring against a real snapshot ------------------------------


def test_replay_succeeds_against_the_real_snapshot(conn, deployment_id):
    deployment = DeploymentRepository(conn).get(deployment_id)
    result = monitoring.replay(conn, deployment)
    assert result is not None
    assert result.resolved.market.name == "nse_equity"
    assert isinstance(result.baseline_trades, list)
    assert isinstance(result.paper_trades, list)


def test_run_end_to_end_produces_a_verdict_and_gate(conn, deployment_id):
    outcome = handler.run(conn, _monitor_job(deployment_id))
    assert outcome.stale is False
    assert outcome.verdict is not None and outcome.verdict.level in ("green", "yellow", "orange", "red")
    assert outcome.gate is not None


def test_persist_records_paper_trades_idempotently(conn, deployment_id):
    outcome = handler.run(conn, _monitor_job(deployment_id))
    with transaction(conn, immediate=True):
        handler.persist(conn, _monitor_job(deployment_id), outcome)
    first_count = len(TradeRepository(conn).for_deployment(deployment_id))

    # A second run against the same (unchanged) data must record nothing new.
    outcome_2 = handler.run(conn, _monitor_job(deployment_id))
    with transaction(conn, immediate=True):
        handler.persist(conn, _monitor_job(deployment_id), outcome_2)
    second_count = len(TradeRepository(conn).for_deployment(deployment_id))

    assert second_count == first_count


# -- persist(): DB writes given a hand-built outcome ----------------------------


def test_persist_green_leaves_deployment_active(conn, deployment_id):
    outcome = _outcome_from_verdict(deployment_id, _verdict("green"))
    with transaction(conn, immediate=True):
        handler.persist(conn, _monitor_job(deployment_id), outcome)

    deployment = DeploymentRepository(conn).get(deployment_id)
    assert deployment["status"] == "active"
    assert deployment["current_health"] == "green"
    assert HealthCheckRepository(conn).latest_for_deployment(deployment_id)["level"] == "green"


def test_persist_red_stops_the_deployment_and_pushes_a_question(conn, deployment_id):
    outcome = _outcome_from_verdict(deployment_id, _verdict("red"))
    with transaction(conn, immediate=True):
        handler.persist(conn, _monitor_job(deployment_id), outcome)

    deployment = DeploymentRepository(conn).get(deployment_id)
    assert deployment["status"] == "stopped"
    assert deployment["current_health"] == "red"

    events = LifecycleEventRepository(conn).find(deployment_id=deployment_id, event_type="health_change")
    assert len(events) == 1

    questions = ResearchQuestionRepository(conn).find(origin_type="live_degradation")
    assert len(questions) == 1


def test_persist_risk_breach_stops_and_discards_trades_after_the_breach_bar(conn, deployment_id):
    """TRD §18: no trade after the breach bar is ever recorded — the kill
    switch is independent of the health verdict entirely."""
    outcome = _outcome_from_verdict(deployment_id, _verdict("green"), breach_index=0)
    with transaction(conn, immediate=True):
        handler.persist(conn, _monitor_job(deployment_id), outcome)

    deployment = DeploymentRepository(conn).get(deployment_id)
    assert deployment["status"] == "stopped"

    kill_events = LifecycleEventRepository(conn).find(deployment_id=deployment_id, event_type="kill_switch")
    assert len(kill_events) == 1
    assert kill_events[0]["triggered_by"] == "automatic_rule"

    # breach_index=0 -> cutoff = paper_result.dates[0] = 2020-01-01; only a
    # trade entered strictly BEFORE that date may be recorded, and neither
    # fixture trade is, so nothing new lands in `trades`.
    assert TradeRepository(conn).for_deployment(deployment_id) == []

    # No promotion row either — a breached run never reaches the gate check.
    assert PromotionRepository(conn).find(strategy_id=deployment["strategy_id"], stage_to="live_small") == []


def test_persist_gate_pass_writes_a_live_small_promotion_once(conn, deployment_id):
    deployment = DeploymentRepository(conn).get(deployment_id)
    passed_gate = monitoring.GateResult(
        passed=True, trade_count_ok=True, deviation_ok=True,
        regime_coverage_ok=True, execution_ok=True, health_ok=True, reasons=(),
    )
    outcome = _outcome_from_verdict(deployment_id, _verdict("green"), gate=passed_gate)

    with transaction(conn, immediate=True):
        handler.persist(conn, _monitor_job(deployment_id), outcome)

    strategy = StrategyRepository(conn).get(deployment["strategy_id"])
    assert strategy["status"] == "pending_live_review"

    promotions = PromotionRepository(conn).find(strategy_id=deployment["strategy_id"], stage_to="live_small")
    assert len(promotions) == 1
    assert promotions[0]["human_decision"] == "pending"

    # A second gate-passing run must not write a second promotion row — the
    # strategy has already left `paper_trading`.
    outcome_2 = _outcome_from_verdict(deployment_id, _verdict("green"), gate=passed_gate)
    with transaction(conn, immediate=True):
        handler.persist(conn, _monitor_job(deployment_id), outcome_2)
    promotions_after = PromotionRepository(conn).find(strategy_id=deployment["strategy_id"], stage_to="live_small")
    assert len(promotions_after) == 1


# -- fire_due_monitor_batch ------------------------------------------------------


def test_fire_due_monitor_batch_fires_one_job_per_active_deployment_and_is_idempotent(
    conn, settings, deployment_id, snapshot_id
):
    # A second strategy/experiment/deployment so there are two active rows.
    strategy_id_2 = StrategyRepository(conn).insert(
        name="orch-fixture-2", family="orch-fixture-family-2",
        market="nse_equity", timeframe="daily", status="evaluating",
    )
    spec_id_2 = SpecRepository(conn).insert_spec(crossover_spec(fast=5, slow=20), strategy_id_2)
    experiments = ExperimentRepository(conn)
    experiment_id_2 = experiments.start(
        strategy_id_2, experiments.next_iteration(strategy_id_2),
        spec_id=spec_id_2, data_snapshot_id=snapshot_id, code_commit="beadfeed",
    )
    promotion_2 = _approve_via_a4(conn, strategy_id_2, experiment_id_2)
    _give_branch(conn, settings, strategy_id_2)
    approved_2 = gates.approve(conn, promotion_2["id"], by="kiran", note="second deployment for the fan-out test")
    deployment_id_2 = approved_2["deployment_id"]

    fired = scheduler.fire_due_monitor_batch(conn)
    assert len(fired) == 2

    # Same tick period: idempotent, no new jobs.
    fired_again = scheduler.fire_due_monitor_batch(conn)
    assert fired_again == fired

    jobs = JobRepository(conn)
    deployment_ids = {jobs.get(job_id)["payload"]["deployment_id"] for job_id in fired}
    assert deployment_ids == {deployment_id, deployment_id_2}


# -- kill / retire ----------------------------------------------------------------


def test_kill_stops_an_active_deployment(conn, deployment_id):
    result = monitoring.kill(conn, deployment_id, by="kiran", reason="manual check")
    assert result["status"] == "stopped"

    deployment = DeploymentRepository(conn).get(deployment_id)
    assert deployment["status"] == "stopped"

    events = LifecycleEventRepository(conn).find(deployment_id=deployment_id, event_type="kill_switch")
    assert len(events) == 1
    assert events[0]["triggered_by"] == "human"


def test_retire_removes_the_file_from_deploy_paper_but_keeps_the_research_branch(conn, settings, deployment_id):
    deployment = DeploymentRepository(conn).get(deployment_id)
    strategy = StrategyRepository(conn).get(deployment["strategy_id"])
    code_path = strategy["code_path"]

    result = monitoring.retire(conn, deployment_id, by="kiran", reason="edge decayed")
    assert result["strategy_status"] == "retired"
    assert result["removal_commit"]

    deployment_after = DeploymentRepository(conn).get(deployment_id)
    assert deployment_after["status"] == "retired"
    assert deployment_after["retirement_reason"] == "edge decayed"

    strategy_after = StrategyRepository(conn).get(strategy["id"])
    assert strategy_after["status"] == "retired"

    events = LifecycleEventRepository(conn).find(deployment_id=deployment_id, event_type="retired")
    assert len(events) == 1
    assert events[0]["evidence"]["removal_commit"] == result["removal_commit"]

    repo_root = settings.strategy_repo_path
    on_deploy = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "deploy/paper"], cwd=repo_root, capture_output=True, text=True
    ).stdout
    on_research = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", strategy["git_branch"]], cwd=repo_root, capture_output=True, text=True
    ).stdout
    assert code_path not in on_deploy.split("\n")
    assert code_path in on_research.split("\n")


def test_retire_requires_a_reason(conn, deployment_id):
    with pytest.raises(ValueError, match="reason"):
        monitoring.retire(conn, deployment_id, by="kiran", reason="  ")

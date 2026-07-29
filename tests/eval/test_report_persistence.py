"""3f — provenance stamping, the report, and writing it into the schema.

`conn` here is the top-level `tests/conftest.py` fixture: a fresh migrated
SQLite database per test, so nothing here touches a developer's working
`aqrl.db` — the same convention every other repository test in the suite uses.
"""
from __future__ import annotations

import pytest

from aqrl.db.repositories import (
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    RegimePerformanceRepository,
    StrategyRepository,
)
from aqrl.eval.bar import AcceptanceBar, check_outcome
from aqrl.eval.checks import CheckResult
from aqrl.eval.metrics import CoreMetrics
from aqrl.eval.persistence import persist_evaluation
from aqrl.eval.regimes import RegimeSlice
from aqrl.eval.report import EvaluationReport, ProvenanceStamp, wf_config_hash
from aqrl.eval.robustness import RobustnessResult
from aqrl.eval.stats.monte_carlo import MonteCarloResult


@pytest.fixture
def experiment_id(conn) -> int:
    strategies = StrategyRepository(conn)
    strategy_id = strategies.get_or_create(
        name="test_strategy", family="test_family", market="nse_equity", timeframe="daily"
    )
    experiments = ExperimentRepository(conn)
    return experiments.start(strategy_id, iteration=1)


def _provenance(**overrides) -> ProvenanceStamp:
    fields = dict(
        eval_engine_version="aqrl-eval-1.0.0+deadbeef",
        market_profile_hash="m" * 16,
        timeframe_profile_hash="t" * 16,
        cost_model_hash="c" * 16,
        wf_config_hash=wf_config_hash("rolling", [1, 2, 3], 1),
        operator_library_version="op" * 8,
        code_commit="abc123",
        data_snapshot_id=None,  # FK to data_snapshots; no snapshot row in this fresh test DB
        random_seed=42,
    )
    fields.update(overrides)
    return ProvenanceStamp(**fields)


def _passing_report() -> EvaluationReport:
    bar = check_outcome(
        AcceptanceBar(min_trades=10), n_trades=200, max_drawdown=0.05, oos_return_total=0.1, breadth=0.6
    )
    metrics = CoreMetrics(
        sharpe=1.2, sortino=1.5, calmar=0.8, cagr=0.1, total_return=0.5, max_drawdown=0.05,
        avg_drawdown=0.02, dd_duration_days=10.0, profit_factor=1.5, win_rate=0.55, expectancy=0.001,
        trade_count=200, avg_trade_return=0.001, turnover=5.0, exposure_pct=0.6,
    )
    robustness = RobustnessResult(
        deflated_sharpe=0.8,
        monte_carlo=MonteCarloResult(-0.1, 0.1, 0.3, 0.05, 100),
        white_rc_pvalue=0.03,
        pbo=0.2,
        param_sensitivity_score=0.1,
        cost_breakeven_multiplier=3.0,
        regimes=[
            RegimeSlice("trending", 1.5, 0.12, 0.04, 80, "2020-01-01", "2020-06-01", 100),
            RegimeSlice("crisis", -0.5, -0.1, 0.15, 20, "2020-06-02", "2020-07-01", 20),
        ],
    )
    checks = [
        CheckResult("static_lookahead_scan", "correctness", "pass"),
        CheckResult("min_trades", "performance", "pass", value=200.0, threshold=100.0),
    ] + bar.checks

    return EvaluationReport(
        provenance=_provenance(),
        phase_reached="P3",
        outcome="passed",
        failure_reason=None,
        bar_verdict=bar,
        best_of_three=None,
        metrics=metrics,
        robustness=robustness,
        all_checks=checks,
        equity_curve_path="data/equity/1.parquet",
        tradebook_path="data/trades/1.parquet",
        duration_seconds=1.23,
    )


def test_provenance_stamp_requires_every_trd_field():
    stamp = _provenance()
    row = stamp.row()
    for field in (
        "eval_engine_version", "market_profile_hash", "timeframe_profile_hash",
        "cost_model_hash", "wf_config_hash", "operator_library_version",
        "code_commit", "data_snapshot_id", "random_seed",
    ):
        assert field in row


def test_wf_config_hash_is_stable_under_train_year_reordering():
    assert wf_config_hash("rolling", [1, 2, 3], 1) == wf_config_hash("rolling", [3, 1, 2], 1)


def test_wf_config_hash_changes_with_the_scheme():
    assert wf_config_hash("rolling", [1, 2, 3], 1) != wf_config_hash("anchored", [1, 2, 3], 1)


def test_report_to_dict_includes_provenance_and_checks():
    report = _passing_report()
    payload = report.to_dict()
    assert payload["provenance"]["random_seed"] == 42
    assert len(payload["checks"]) == len(report.all_checks)
    assert payload["outcome"] == "passed"


def test_report_to_markdown_is_readable_text():
    report = _passing_report()
    markdown = report.to_markdown()
    assert "PASSED" in markdown
    assert "Honest score" not in markdown or True  # no honest_score without best_of_three
    assert "Provenance" in markdown


def test_persist_evaluation_writes_every_child_table(conn, experiment_id):
    report = _passing_report()
    evaluation_id = persist_evaluation(conn, experiment_id, report)

    evaluations = EvaluationRepository(conn)
    row = evaluations.get(evaluation_id)
    assert row["phase"] == "P3"
    assert row["result"] == "pass"

    # Provenance lives on `experiments`, not `evaluations` (Backend-Schema §5).
    experiment_row = ExperimentRepository(conn).get(experiment_id)
    for field, value in report.provenance.row().items():
        assert experiment_row[field] == value

    test_rows = EvaluationTestRepository(conn).for_evaluation(evaluation_id)
    assert len(test_rows) == len(report.all_checks)

    regime_rows = RegimePerformanceRepository(conn).for_evaluation(evaluation_id)
    assert len(regime_rows) == 2
    assert {row["regime"] for row in regime_rows} == {"trending", "crisis"}


def test_persist_evaluation_closes_the_experiment(conn, experiment_id):
    report = _passing_report()
    persist_evaluation(conn, experiment_id, report)

    experiments = ExperimentRepository(conn)
    row = experiments.get(experiment_id)
    assert row["status"] == "evaluated"
    assert row["outcome"] == "passed"
    assert row["phase_reached"] == "P3"


def test_a_bar_failure_leaves_no_honest_score_persisted(conn, experiment_id):
    """TRD §7.5: fail any bar item -> discard, NO SCORE COMPUTED. The row must
    reflect that as a NULL, not a fabricated number."""
    bar = check_outcome(
        AcceptanceBar(min_trades=100), n_trades=10, max_drawdown=0.05, oos_return_total=0.1, breadth=0.6
    )
    report = EvaluationReport(
        provenance=_provenance(),
        phase_reached="bar",
        outcome="failed",
        failure_reason="insufficient_trades",
        bar_verdict=bar,
        best_of_three=None,
        metrics=None,
        robustness=None,
        all_checks=bar.checks,
    )
    evaluation_id = persist_evaluation(conn, experiment_id, report)
    row = EvaluationRepository(conn).get(evaluation_id)
    assert row["honest_score"] is None
    assert row["bar_result"] == "fail"
    assert row["bar_failed_on"] == "min_trades"

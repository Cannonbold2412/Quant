"""The `EVALUATE` handler: rebuilding inputs from the database, and the
bar-clear short-circuit (App-Flow §6.1) — routing straight to `PROMOTE` or
`REVIEW` without ever invoking A3, which does not exist yet.
"""
from __future__ import annotations

import pytest

from aqrl.db import transaction
from aqrl.db.repositories import EvaluationRepository, JobRepository, StrategyRepository
from aqrl.eval.bar import AcceptanceBar, check_outcome
from aqrl.eval.report import EvaluationReport, ProvenanceStamp, wf_config_hash
from aqrl.orchestration.handlers import evaluate as handler
from aqrl.orchestration.worker import run_job


def _provenance(**overrides) -> ProvenanceStamp:
    fields = dict(
        eval_engine_version="test-engine-1.0.0",
        market_profile_hash="m" * 16,
        timeframe_profile_hash="t" * 16,
        cost_model_hash="c" * 16,
        wf_config_hash=wf_config_hash("rolling", [1, 2, 3], 1),
        operator_library_version="op" * 8,
        code_commit="abc123",
        data_snapshot_id=None,
        random_seed=1,
    )
    fields.update(overrides)
    return ProvenanceStamp(**fields)


def _report(*, passed: bool) -> EvaluationReport:
    bar = check_outcome(
        AcceptanceBar(min_trades=1),
        n_trades=10,
        max_drawdown=0.05,
        oos_return_total=(0.1 if passed else -0.1),
        breadth=0.6,
    )
    assert bar.passed is passed
    return EvaluationReport(
        provenance=_provenance(),
        phase_reached="P3",
        outcome="passed" if passed else "failed",
        failure_reason=None if passed else "costs_exceed_edge",
        bar_verdict=bar,
        best_of_three=None,
        metrics=None,
        robustness=None,
        all_checks=bar.checks,
    )


def test_persist_routes_bar_clear_to_promote(conn, strategy_id, experiment_id):
    outcome = handler.EvaluateOutcome(experiment_id=experiment_id, strategy_id=strategy_id, report=_report(passed=True))
    with transaction(conn, immediate=True):
        handler.persist(conn, {}, outcome)

    jobs = JobRepository(conn)
    promote_jobs = jobs.find(job_type="PROMOTE")
    assert len(promote_jobs) == 1
    assert promote_jobs[0]["experiment_id"] == experiment_id
    assert jobs.find(job_type="REVIEW") == []

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "pending_promotion"
    assert strategy["best_experiment_id"] == experiment_id


def test_persist_routes_bar_failure_to_review(conn, strategy_id, experiment_id):
    outcome = handler.EvaluateOutcome(experiment_id=experiment_id, strategy_id=strategy_id, report=_report(passed=False))
    with transaction(conn, immediate=True):
        handler.persist(conn, {}, outcome)

    jobs = JobRepository(conn)
    assert len(jobs.find(job_type="REVIEW")) == 1
    assert jobs.find(job_type="PROMOTE") == []

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["plateau_counter"] == 1
    assert strategy["status"] != "pending_promotion"


def test_persist_writes_the_evaluation_regardless_of_verdict(conn, strategy_id, experiment_id):
    outcome = handler.EvaluateOutcome(experiment_id=experiment_id, strategy_id=strategy_id, report=_report(passed=False))
    with transaction(conn, immediate=True):
        handler.persist(conn, {}, outcome)

    evaluation = EvaluationRepository(conn).latest_for_experiment(experiment_id)
    assert evaluation is not None
    assert evaluation["result"] == "fail"


# -- run(): rebuilding EvaluationInputs from the database ------------------------


def test_run_requires_experiment_id():
    with pytest.raises(ValueError, match="experiment_id"):
        handler.run(None, {"experiment_id": None, "payload": {}})


def test_run_requires_asset_class_in_payload(conn, experiment_id):
    job = {"experiment_id": experiment_id, "payload": {}}
    with pytest.raises(ValueError, match="asset_class"):
        handler.run(conn, job)


# -- end to end, through the real queue and worker ------------------------------


def test_evaluate_job_runs_end_to_end_through_the_queue(conn, strategy_id, experiment_id, evaluate_job_id):
    """The full DB-driven path: claim, run the real engine, persist, close
    the job out. Whatever research phase the synthetic data happens to reach
    is not the point here — `queued vs. direct call` determinism and the
    crash-recovery tests cover that; this just proves the plumbing holds
    together end to end without raising."""
    jobs = JobRepository(conn)
    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=120)
    assert claimed["id"] == evaluate_job_id

    code = run_job(claimed["uid"], worker_id="w1")
    assert code == 0

    job = jobs.get(evaluate_job_id)
    assert job["status"] == "succeeded"
    assert job["duration_seconds"] is not None
    assert EvaluationRepository(conn).latest_for_experiment(experiment_id) is not None


def test_run_rebuilt_from_the_database_is_deterministic(conn, experiment_id, evaluate_job_id):
    """The engine's own determinism is Stage 3's tested contract
    (`tests/eval/test_engine.py::test_report_is_deterministic_for_the_same_seed`).
    What Stage 4 adds is reconstructing `EvaluationInputs` from database rows
    rather than receiving them directly — this checks that reconstruction is
    itself stable, so running the *same* job through `handler.run()` twice
    (e.g. once for real, once on a retry after a crash before persistence)
    produces bit-identical reports."""
    job = JobRepository(conn).get(evaluate_job_id)

    first = handler.run(conn, job)
    second = handler.run(conn, job)

    assert first.report.outcome == second.report.outcome
    assert first.report.phase_reached == second.report.phase_reached

    # `duration_seconds` is wall-clock timing metadata, not a research
    # result — it is expected to vary between runs even when everything
    # else is bit-identical.
    def _without_duration(payload: dict) -> dict:
        payload = dict(payload)
        payload["evaluation"] = {k: v for k, v in payload["evaluation"].items() if k != "duration_seconds"}
        return payload

    assert _without_duration(first.report.to_dict()) == _without_duration(second.report.to_dict())

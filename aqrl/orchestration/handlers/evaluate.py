"""The `EVALUATE` handler — Stage 4's one real payload for the queue.

Rebuilds `EvaluationInputs` from what a queued job actually carries (a
database row and a payload dict) rather than the argv Stage 3's
`aqrl evaluate run` CLI takes, then calls the same `evaluate_experiment` /
`persist_evaluation` Stage 3 already ships — this module is glue, not a
second evaluation path (TRD §6.1 forbids forking the engine, and that applies
to how it's *invoked* just as much as to its internals).

The bar-clear short-circuit (App-Flow §6.1) lives here: `persist()` looks at
`report.bar_verdict` and enqueues `PROMOTE` or `REVIEW` accordingly, in the
same transaction as the evaluation write. A3 (Stage 6) is never invoked from
this module — it isn't built yet, and the routing decision doesn't need it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ...data.snapshots import SnapshotManager
from ...db.repositories import ExperimentRepository, SpecRepository, SnapshotRepository, StrategyRepository
from ...db.repositories.base import Row
from ...eval.bar import AcceptanceBar
from ...eval.engine import EvaluationInputs, evaluate_experiment
from ...eval.panel import PricePanel
from ...eval.persistence import persist_evaluation
from ...eval.report import EvaluationReport
from ...profiles import ProfileLoader
from ..events import Event, emit
from .base import HandlerResult

__all__ = ["EvaluateOutcome", "persist", "run"]


@dataclass(frozen=True)
class EvaluateOutcome:
    experiment_id: int
    strategy_id: int
    report: EvaluationReport


def run(conn: sqlite3.Connection, job: Row) -> EvaluateOutcome:
    """The heavy, side-effect-free half: build inputs, run the engine.

    Only reads the database (no open transaction required for that in
    SQLite); every write happens in `persist()`.
    """
    experiment_id = job["experiment_id"]
    if experiment_id is None:
        raise ValueError("EVALUATE job has no experiment_id")

    experiments = ExperimentRepository(conn)
    experiment = experiments.get(experiment_id)
    if experiment is None:
        raise ValueError(f"no experiment {experiment_id}")

    strategies = StrategyRepository(conn)
    strategy = strategies.get(experiment["strategy_id"])
    if strategy is None:
        raise ValueError(f"no strategy {experiment['strategy_id']}")

    if experiment["spec_id"] is None:
        raise ValueError(f"experiment {experiment_id} has no spec_id")
    spec = SpecRepository(conn).load_spec(experiment["spec_id"])

    payload = job["payload"] or {}
    asset_class = payload.get("asset_class")
    if not asset_class:
        raise ValueError("EVALUATE job payload missing required 'asset_class'")

    resolved = ProfileLoader().resolve(strategy["market"], strategy["timeframe"], asset_class)

    snapshot_id = experiment["data_snapshot_id"] or payload.get("data_snapshot_id")
    if snapshot_id is None:
        raise ValueError("EVALUATE job has no data_snapshot_id (experiment nor payload)")

    frame = SnapshotManager(conn).load(snapshot_id)
    panel = PricePanel.from_frame(frame)

    inputs = EvaluationInputs(
        spec=spec,
        panel=panel,
        resolved=resolved,
        snapshot=SnapshotRepository(conn).get(snapshot_id),
        acceptance_bar=AcceptanceBar.locked(conn, payload.get("campaign")),
        family_prior_trials=strategies.family_trial_count(strategy["family"]),
        code_commit=experiment["code_commit"] or payload.get("code_commit"),
        data_snapshot_id=snapshot_id,
        random_seed=payload.get("random_seed", experiment["iteration"]),
        cost_multiplier=payload.get("cost_multiplier", 2.0),
    )
    report = evaluate_experiment(inputs)
    return EvaluateOutcome(experiment_id=experiment_id, strategy_id=strategy["id"], report=report)


def persist(conn: sqlite3.Connection, job: Row, outcome: EvaluateOutcome) -> HandlerResult:
    """The short, atomic half: write the report, route on the bar verdict."""
    evaluation_id = persist_evaluation(conn, outcome.experiment_id, outcome.report)

    verdict = outcome.report.bar_verdict
    if verdict is not None:
        strategies = StrategyRepository(conn)
        if verdict.passed:
            score = outcome.report.honest_score.honest_score if outcome.report.honest_score else 0.0
            strategies.record_bar_clear(outcome.strategy_id, outcome.experiment_id, score)
            event = Event.EVALUATION_CLEARED_BAR
        else:
            strategies.record_bar_failure(outcome.strategy_id)
            event = Event.EVALUATION_FAILED_BAR
        emit(
            conn,
            event,
            strategy_id=outcome.strategy_id,
            experiment_id=outcome.experiment_id,
            payload={"evaluation_id": evaluation_id},
        )

    return HandlerResult(tokens_spent=0)

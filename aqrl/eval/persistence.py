"""Writing a report into the schema Stage 1 already migrated.

One function, one transaction: an `EvaluationReport` becomes one `evaluations`
row, its checks become `evaluation_tests` rows, its regime slices become
`regime_performance` rows, and the parent `experiments` row is closed **and
stamped with its provenance**. Nothing here decides *what* the outcome was —
`engine.py` decides that; this module only makes it durable.

**Provenance lives on `experiments`, not `evaluations`** (Backend-Schema §5) —
`code_commit`, `data_snapshot_id` and friends describe the experiment as a
whole, not one phase run of it. Writing it here, at completion, guarantees the
nine TRD §6.6 fields land on every experiment this module closes, regardless of
whether the caller also set them at `ExperimentRepository.start`.

**Every `evaluation_tests` row is written, pass or fail.** A passing test with
no stored row is unverifiable a week later; the schema exists so *"which
specific gate failed, and by how much"* is a query, not a memory (TRD §10.3).
"""
from __future__ import annotations

import sqlite3

from ..db.repositories import (
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    RegimePerformanceRepository,
)
from .report import EvaluationReport

__all__ = ["persist_evaluation"]

#: `experiments.status` / `.outcome` for each report outcome. Mirrors
#: `VERDICT_TO_STATUS` (`aqrl/db/repositories/research.py`) but keyed on the
#: engine's own vocabulary rather than nanoAQRL's three-word one.
_OUTCOME_TO_STATUS = {
    "passed": "evaluated",
    "failed": "evaluated",
    "error": "error",
}


def persist_evaluation(conn: sqlite3.Connection, experiment_id: int, report: EvaluationReport) -> int:
    """Write one report, close its experiment, return the `evaluations` row id."""
    evaluations = EvaluationRepository(conn)
    tests = EvaluationTestRepository(conn)
    regimes = RegimePerformanceRepository(conn)
    experiments = ExperimentRepository(conn)

    evaluation_id = evaluations.insert(experiment_id=experiment_id, **report.evaluation_row())

    for check in report.all_checks:
        tests.insert(evaluation_id=evaluation_id, **check.row())

    if report.robustness is not None:
        for regime in report.robustness.regimes:
            regimes.insert(
                evaluation_id=evaluation_id,
                regime=regime.regime,
                sharpe=regime.sharpe,
                cagr=regime.cagr,
                max_drawdown=regime.max_drawdown,
                trade_count=regime.trade_count,
                period_start=regime.period_start,
                period_end=regime.period_end,
            )

    experiments.update(experiment_id, **report.provenance.row())
    experiments.complete(
        experiment_id,
        status=_OUTCOME_TO_STATUS[report.outcome],
        outcome=report.outcome,
        phase_reached=report.phase_reached,
        failure_reason=report.failure_reason,
    )
    return evaluation_id

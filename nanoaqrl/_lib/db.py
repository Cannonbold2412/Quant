"""nanoAQRL's persistence — an adapter over the canonical `aqrl.db` schema.

Stage 0 shipped its own `schema.sql` with five tables and its own column names.
Stage 1 makes `aqrl/` the single owner of the schema, so this module keeps its
original function signatures (nothing in `evaluate.py` had to change shape) but
writes through `aqrl.db.repositories` into the canonical tables.

Two translations happen here, and only here:

**Verdicts vs lifecycle status.** `results.tsv` records exactly three verdicts —
`keep` / `discard` / `crash` (TRD §2.2). Those are a *verdict*, not a lifecycle
state, so they map onto Backend-Schema §14.2's `status` plus `outcome` rather
than replacing them.

**Failure reasons.** Backend-Schema §14.5 is explicit that free text is not
acceptable in `failure_reason`, and that the three bug categories route back to
A2 and must never be recorded as research conclusions. Stage 0's ad-hoc strings
are mapped onto that enum below.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from aqrl.db import connect as _connect
from aqrl.db import migrate as _migrate
from aqrl.db.repositories import (
    VERDICT_TO_STATUS,
    EvaluationRepository,
    ExperimentRepository,
    NullWorldRunRepository,
    StrategyRepository,
)

# Stage 0's strings -> Backend-Schema §14.5. The first three are BUGS, not
# research findings, and are routed as such.
FAILURE_REASONS: dict[str, str | None] = {
    "lookahead_static": "look_ahead_detected",
    "lookahead_empirical": "data_leakage_detected",
    "commit_not_found": "code_error",
    # Bar failures. `evaluations.bar_failed_on` records which gate failed
    # precisely; these map onto the nearest research-finding category.
    "min_trades": "insufficient_trades",
    "cost_stress": "costs_exceed_edge",
    "min_score": "deflated_sharpe_insufficient",
    # Drawdown has no research-finding category — it gates but does not rank,
    # being too noisy a worst-moment statistic (TRD §7.5). `bar_failed_on`
    # carries it rather than forcing it into an ill-fitting enum value.
    "max_drawdown": None,
}

# Stage 0's evaluation kwargs -> canonical `evaluations` columns.
EVALUATION_COLUMNS: dict[str, str] = {
    "n_trials": "n_trials_used",
    "score_1yr": "score_train_1y",
    "score_2yr": "score_train_2y",
    "score_3yr": "score_train_3y",
    "n_trades": "trade_count",
}

NULL_MODELS: dict[str, str] = {
    "permuted": "permuted_returns",
    "permuted_returns": "permuted_returns",
    "block_bootstrap": "block_bootstrap",
    "synthetic_path": "synthetic_fat_tail",
    "synthetic_fat_tail": "synthetic_fat_tail",
    "synthetic_gbm": "synthetic_gbm",
}


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open the canonical database, applying any pending migrations."""
    conn = _connect(db_path)
    _migrate(conn)
    return conn


def get_or_create_strategy(
    conn: sqlite3.Connection,
    name: str,
    family: str,
    market: str,
    timeframe: str,
    git_branch: str | None = None,
    code_path: str | None = None,
) -> int:
    return StrategyRepository(conn).get_or_create(
        name=name,
        family=family,
        market=market,
        timeframe=timeframe,
        git_branch=git_branch,
        code_path=code_path,
    )


def next_iteration(conn: sqlite3.Connection, strategy_id: int) -> int:
    return ExperimentRepository(conn).next_iteration(strategy_id)


def get_family_trial_count(conn: sqlite3.Connection, family: str) -> int:
    """Prior iterations across every strategy in this family.

    Returns the RAW count — the caller adds this run's own attempt and
    `run_best_of_three` applies the x3 selection factor exactly once, so the
    multiplier is never applied twice (TRD §8.6).
    """
    return StrategyRepository(conn).family_trial_count(family)


def insert_experiment(
    conn: sqlite3.Connection,
    strategy_id: int,
    iteration: int,
    code_commit: str,
    wf_config_hash: str,
    eval_engine_version: str,
    random_seed: int,
    **provenance: Any,
) -> int:
    return ExperimentRepository(conn).start(
        strategy_id,
        iteration,
        code_commit=code_commit,
        wf_config_hash=wf_config_hash,
        eval_engine_version=eval_engine_version,
        random_seed=random_seed,
        **provenance,
    )


def complete_experiment(
    conn: sqlite3.Connection,
    experiment_id: int,
    status: str,
    phase_reached: str,
    outcome: str,
    failure_reason: str | None = None,
) -> None:
    """Close an experiment from a nanoAQRL verdict.

    `status` here is the `results.tsv` verdict; the canonical status/outcome
    pair is derived from it, so the two can never drift apart.
    """
    canonical_status, canonical_outcome = VERDICT_TO_STATUS[status]
    if failure_reason is not None and failure_reason not in FAILURE_REASONS:
        raise ValueError(
            f"unmapped failure reason {failure_reason!r}. Backend-Schema §14.5 does not accept free "
            "text here — add an explicit mapping rather than inventing a category."
        )
    ExperimentRepository(conn).complete(
        experiment_id,
        status=canonical_status,
        outcome=canonical_outcome,
        phase_reached=phase_reached,
        failure_reason=FAILURE_REASONS.get(failure_reason) if failure_reason else None,
    )


def record_bar_clear(
    conn: sqlite3.Connection, strategy_id: int, experiment_id: int, score: float
) -> None:
    StrategyRepository(conn).record_bar_clear(strategy_id, experiment_id, score)


def record_plateau_step(conn: sqlite3.Connection, strategy_id: int, cleared: bool) -> int:
    """Advance the plateau counter and return it.

    Clearing the bar is an immediate, unconditional stop, so there is no
    "improvement" case that resets the counter — a cleared bar simply reports
    the current value and the loop ends (Backend-Schema §4).
    """
    strategies = StrategyRepository(conn)
    if cleared:
        row = strategies.get(strategy_id)
        return int(row["plateau_counter"]) if row else 0
    return strategies.record_bar_failure(strategy_id)


def insert_evaluation(
    conn: sqlite3.Connection, experiment_id: int, phase: str, result: str, **kwargs: Any
) -> int:
    renamed = {EVALUATION_COLUMNS.get(key, key): value for key, value in kwargs.items()}
    return EvaluationRepository(conn).insert(
        experiment_id=experiment_id, phase=phase, result=result, **renamed
    )


def insert_null_world_run(
    conn: sqlite3.Connection,
    generator: str,
    n_replications: int,
    n_discoveries: int,
    max_score_observed: float,
    eval_engine_version: str,
) -> int:
    """Record a calibration run. FDR and verdict are derived, never passed in."""
    return NullWorldRunRepository(conn).record(
        null_model=NULL_MODELS.get(generator, generator),
        replications=n_replications,
        discoveries_reported=n_discoveries,
        max_score_observed=max_score_observed,
        eval_engine_version=eval_engine_version,
    )


def append_results_tsv(
    path: str | Path, commit: str, score: Any, n_trades: int, status: str, description: str
) -> None:
    """Append one row to `results.tsv`. Append only, one verdict per experiment."""
    path = Path(path)
    is_new = not path.exists()
    with path.open("a") as handle:
        if is_new:
            handle.write("commit\tscore\tn_trades\tstatus\tdescription\n")
        score_str = "" if score is None else f"{score:.6f}"
        handle.write(f"{commit}\t{score_str}\t{n_trades}\t{status}\t{description}\n")

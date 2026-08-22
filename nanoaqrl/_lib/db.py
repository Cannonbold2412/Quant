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
from collections.abc import Sequence
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
    # The ratchet: cleared every validity floor but did not beat the best score
    # this strategy has already recorded. The bar it failed against *is* the
    # running best, so `plateaued_below_bar` is the literal description.
    "baseline": "plateaued_below_bar",
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


def best_score(conn: sqlite3.Connection, strategy_id: int) -> float | None:
    """The ratchet's baseline: the best honest score this strategy has recorded,
    or `None` if it has never cleared the bar.

    Deliberately read from `strategies.best_score` rather than a side-car
    `.baseline.json`. That column is already written on every keep, so a second
    copy on disk could only ever drift out of step with it — and unlike a single
    global file it is scoped per strategy, which is the scope a baseline
    actually has.
    """
    row = StrategyRepository(conn).get(strategy_id)
    if row is None or row["best_score"] is None:
        return None
    return float(row["best_score"])


def record_improvement(
    conn: sqlite3.Connection, strategy_id: int, experiment_id: int, score: float
) -> None:
    """Advance the ratchet: this experiment is the new best.

    Distinct from `StrategyRepository.record_bar_clear`, which also flips the
    strategy to `pending_promotion` because clearing a *fixed* bar ends the
    loop. Under a ratchet nothing is ever final — beating the previous best
    raises the bar and the loop keeps going — so the strategy stays
    `iterating`, and reaching promotion becomes the plateau counter's job.
    """
    StrategyRepository(conn).update(
        strategy_id,
        best_experiment_id=experiment_id,
        best_score=score,
        status="iterating",
    )


def record_plateau_step(conn: sqlite3.Connection, strategy_id: int, cleared: bool) -> int:
    """Advance the plateau counter and return it.

    Under the ratchet a keep is an *improvement*, not a stop, so it resets the
    counter to zero — the column now means "consecutive experiments that failed
    to improve", which is the only remaining signal that the search is done.
    """
    strategies = StrategyRepository(conn)
    if cleared:
        strategies.update(strategy_id, plateau_counter=0)
        return 0
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


def append_results_tsv(path: str | Path, row: dict[str, Any], columns: Sequence[str]) -> None:
    """Append one row to `results.tsv`. Append only, one verdict per experiment.

    `columns` is the caller's schema; missing keys are written empty rather than
    raising, so a crash row can carry only the handful of fields it knows.

    If the file on disk was written under a *different* schema, it is rotated to
    `.bak` and started fresh. Appending a wide row under a narrow header would
    produce a file that still parses — silently, with every column after the
    fifth misaligned — and this is the run log the whole loop is read from.
    """
    path = Path(path)
    header = "\t".join(columns)
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            existing = handle.readline().rstrip("\n")
        if existing and existing != header:
            path.replace(path.with_suffix(path.suffix + ".bak"))
    is_new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        if is_new:
            handle.write(header + "\n")
        # A tab or newline inside a free-text description would shift every
        # later column of that row; TSV has no quoting to fall back on.
        cells = (str(row.get(column, "")).replace("\t", " ").replace("\n", " ") for column in columns)
        handle.write("\t".join(cells) + "\n")

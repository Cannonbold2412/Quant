"""Repositories for the research loop: strategies, experiments, evaluations.

These carry the queries Backend-Schema §15 says must be fast, most importantly
`family_trial_count` — the deflated Sharpe is only honest if the trial count
behind it is exact, and *"how many attempts in this family?"* is precisely the
question git cannot answer (TRD §5.1).
"""
from __future__ import annotations

from typing import Any

from .base import Repository, Row, utcnow_iso

# nanoAQRL's `results.tsv` records exactly three verdicts (TRD §2.2). They are a
# *verdict*, not a lifecycle state, so they map onto the canonical
# `experiments.status` / `outcome` pair rather than replacing it.
VERDICT_TO_STATUS: dict[str, tuple[str, str]] = {
    "keep": ("evaluated", "passed"),
    "discard": ("evaluated", "failed"),
    "crash": ("error", "error"),
}


class StrategyRepository(Repository):
    table = "strategies"
    updated_column = "updated_at"

    def get_or_create(
        self,
        name: str,
        family: str,
        market: str,
        timeframe: str,
        **fields: Any,
    ) -> int:
        existing = self.conn.execute(
            "SELECT id FROM strategies WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            return int(existing["id"])
        return self.insert(name=name, family=family, market=market, timeframe=timeframe, **fields)

    def family_trial_count(self, family: str) -> int:
        """Prior iterations across every strategy in this family.

        Backend-Schema §4: *"the same idea tried in 3 markets is 3 trials in one
        family, not 3 independent results."* Returns the RAW count — the caller
        adds the current attempt and applies the best-of-three x3 factor exactly
        once, so the multiplier is never applied twice (TRD §8.6).
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(iteration_count), 0) AS total FROM strategies WHERE family = ?",
            (family,),
        ).fetchone()
        return int(row["total"])

    def record_bar_clear(self, strategy_id: int, experiment_id: int, score: float) -> None:
        """Clearing the bar is an immediate, unconditional stop (PRD §9.2)."""
        self.update(
            strategy_id,
            best_experiment_id=experiment_id,
            best_score=score,
            status="pending_promotion",
        )

    def record_bar_failure(self, strategy_id: int) -> int:
        """Increment the plateau counter and return its new value.

        Only ever increments. Clearing the bar stops the loop immediately, so
        there is no "improvement" case to reset against (Backend-Schema §4).
        """
        self.conn.execute(
            "UPDATE strategies SET plateau_counter = plateau_counter + 1, updated_at = ? WHERE id = ?",
            (utcnow_iso(), strategy_id),
        )
        row = self.conn.execute(
            "SELECT plateau_counter FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        return int(row["plateau_counter"])


class ExperimentRepository(Repository):
    table = "experiments"
    json_columns = frozenset({"metrics_json"})

    def next_iteration(self, strategy_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(iteration), 0) AS max_iter FROM experiments WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        return int(row["max_iter"]) + 1

    def start(self, strategy_id: int, iteration: int, **provenance: Any) -> int:
        """Open an experiment row before doing the work.

        It starts in `evaluating`: if the process dies mid-run the row is
        visibly unfinished rather than absent, which is what makes a crash
        distinguishable from "never happened".
        """
        return self.insert(
            strategy_id=strategy_id, iteration=iteration, status="evaluating", **provenance
        )

    def complete(
        self,
        experiment_id: int,
        status: str,
        outcome: str,
        phase_reached: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        self.update(
            experiment_id,
            status=status,
            outcome=outcome,
            phase_reached=phase_reached,
            failure_reason=failure_reason,
            completed_at=utcnow_iso(),
        )
        self.conn.execute(
            """UPDATE strategies
                  SET iteration_count = iteration_count + 1, updated_at = ?
                WHERE id = (SELECT strategy_id FROM experiments WHERE id = ?)""",
            (utcnow_iso(), experiment_id),
        )

    def complete_from_verdict(
        self,
        experiment_id: int,
        verdict: str,
        phase_reached: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Close an experiment from a nanoAQRL `keep`/`discard`/`crash` verdict."""
        try:
            status, outcome = VERDICT_TO_STATUS[verdict]
        except KeyError:
            raise ValueError(f"unknown verdict {verdict!r}; expected one of {sorted(VERDICT_TO_STATUS)}") from None
        self.complete(experiment_id, status, outcome, phase_reached, failure_reason)

    def comparable_to(
        self,
        eval_engine_version: str,
        market_profile_hash: str,
        timeframe_profile_hash: str,
        wf_config_hash: str,
    ) -> list[Row]:
        """Backend-Schema §15 Q2 — *"which stored results are still comparable?"*

        Answered from `idx_experiments_provenance`, never a scan. Without this,
        the knowledge base silently mixes results scored under different rules.
        """
        rows = self.conn.execute(
            """SELECT * FROM experiments
                WHERE eval_engine_version = ? AND market_profile_hash = ?
                  AND timeframe_profile_hash = ? AND wf_config_hash = ?""",
            (eval_engine_version, market_profile_hash, timeframe_profile_hash, wf_config_hash),
        ).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[misc]

    def mark_incomparable(self, **provenance: Any) -> int:
        """Flag stored results as no longer comparable. They are never deleted."""
        clauses = " AND ".join(f"{column} = ?" for column in provenance)
        cursor = self.conn.execute(
            f"UPDATE experiments SET comparable = 0 WHERE {clauses}", list(provenance.values())
        )
        return cursor.rowcount


class EvaluationRepository(Repository):
    table = "evaluations"
    json_columns = frozenset(
        {"wf_train_years_evaluated", "tuned_params_per_fold", "fold_metrics", "metrics_json"}
    )

    def for_experiment(self, experiment_id: int) -> list[Row]:
        return self.find(experiment_id=experiment_id, order_by="id")

    def latest_for_experiment(self, experiment_id: int, phase: str | None = None) -> Row | None:
        sql = "SELECT * FROM evaluations WHERE experiment_id = ?"
        params: list[Any] = [experiment_id]
        if phase:
            sql += " AND phase = ?"
            params.append(phase)
        sql += " ORDER BY id DESC LIMIT 1"
        return self._decode(self.conn.execute(sql, params).fetchone())


class EvaluationTestRepository(Repository):
    """One row per test within a phase — `evaluation_tests` has no `uid` or
    `created_at`, so both Repository conventions are switched off."""

    table = "evaluation_tests"
    has_uid = False
    created_column = None

    def for_evaluation(self, evaluation_id: int) -> list[Row]:
        return self.find(evaluation_id=evaluation_id)


class RegimePerformanceRepository(Repository):
    """Per-regime performance rows — same no-`uid`/no-timestamp shape."""

    table = "regime_performance"
    has_uid = False
    created_column = None

    def for_evaluation(self, evaluation_id: int) -> list[Row]:
        return self.find(evaluation_id=evaluation_id)


class NullWorldRunRepository(Repository):
    table = "null_world_runs"

    def record(
        self,
        null_model: str,
        replications: int,
        discoveries_reported: int,
        max_score_observed: float,
        **fields: Any,
    ) -> int:
        """Record a calibration run, deriving FDR and the verdict.

        Milestone 0: *"a discovery from an uncalibrated pipeline is not a
        discovery."* The rate is derived here rather than passed in so it can
        never disagree with the counts beside it.
        """
        rate = discoveries_reported / replications if replications else 0.0
        fields.setdefault("verdict", "pipeline_trusted" if discoveries_reported == 0 else "pipeline_suspect")
        return self.insert(
            null_model=null_model,
            replications=replications,
            discoveries_reported=discoveries_reported,
            false_discovery_rate=rate,
            max_score_observed=max_score_observed,
            **fields,
        )

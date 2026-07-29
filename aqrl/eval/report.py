"""The provenance stamp and the report — TRD §6.6, §6.7.

> *"The day cost assumptions change — and they will — we must instantly answer
> 'which of my 40,000 stored results are still comparable?' Without this, the
> knowledge base silently mixes results scored under different rules."*

Nine fields, always stamped, never optional:

    eval_engine_version   market_profile_hash    timeframe_profile_hash
    cost_model_hash        wf_config_hash          code_commit
    data_snapshot_id       operator_library_version  random_seed

`ProvenanceStamp` exists so a caller cannot accidentally omit one — every field
is required, not defaulted, so a missing value is a constructor error rather
than a silent `NULL` discovered three months into a campaign.

The report itself is two renderings of the same data: `to_dict()` for the
database and any downstream tooling, `to_markdown()` for the human reading the
result of one experiment. Neither is the record of truth — the `evaluations`
row is — but both are derived from exactly the values written there, so they
cannot drift from what was actually stored.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..hashing import canonical_json
from .bar import BarVerdict
from .checks import CheckResult
from .metrics import CoreMetrics
from .regimes import RegimeSlice
from .robustness import RobustnessResult
from .stats.honest_score import HonestScoreResult
from .walk_forward import BestOfThreeResult

__all__ = ["EvaluationReport", "ProvenanceStamp", "wf_config_hash"]


@dataclass(frozen=True)
class ProvenanceStamp:
    """The nine fields TRD §6.6 requires on every experiment. All required."""

    eval_engine_version: str
    market_profile_hash: str
    timeframe_profile_hash: str
    cost_model_hash: str
    wf_config_hash: str
    operator_library_version: str
    code_commit: str | None
    data_snapshot_id: int | None
    random_seed: int

    def row(self) -> dict[str, Any]:
        return {
            "eval_engine_version": self.eval_engine_version,
            "market_profile_hash": self.market_profile_hash,
            "timeframe_profile_hash": self.timeframe_profile_hash,
            "cost_model_hash": self.cost_model_hash,
            "wf_config_hash": self.wf_config_hash,
            "operator_library_version": self.operator_library_version,
            "code_commit": self.code_commit,
            "data_snapshot_id": self.data_snapshot_id,
            "random_seed": self.random_seed,
        }


def wf_config_hash(scheme: str, train_years: list[int], test_years: int) -> str:
    """Hash of `{scheme, train_years, test_years}` (TRD §6.6, §8.2).

    Train length carries the same hidden-multiple-testing risk as scheme
    choice, so both are in the hash — a campaign that quietly changed its
    walk-forward configuration must not compare against one that did not.
    """
    payload = canonical_json(
        {"scheme": scheme, "train_years": sorted(train_years), "test_years": test_years}
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class EvaluationReport:
    """Everything one `EVALUATE` job produced, in the shape the schema wants."""

    provenance: ProvenanceStamp
    phase_reached: str
    outcome: str  # 'passed' | 'failed' | 'error'
    failure_reason: str | None
    bar_verdict: BarVerdict | None
    best_of_three: BestOfThreeResult | None
    metrics: CoreMetrics | None
    robustness: RobustnessResult | None
    all_checks: list[CheckResult] = field(default_factory=list)
    equity_curve_path: str | None = None
    tradebook_path: str | None = None
    duration_seconds: float | None = None
    timed_out: bool = False

    @property
    def honest_score(self) -> HonestScoreResult | None:
        return self.best_of_three.score if self.best_of_three else None

    def evaluation_row(self) -> dict[str, Any]:
        """The `evaluations` table row. `None` for anything a failed phase
        never reached — never a fabricated zero."""
        row: dict[str, Any] = {
            "phase": self.phase_reached,
            "result": "pass" if self.outcome == "passed" else "fail",
        }

        if self.bar_verdict is not None:
            row["bar_result"] = "pass" if self.bar_verdict.passed else "fail"
            row["bar_failed_on"] = self.bar_verdict.failed_on

        if self.best_of_three is not None:
            score = self.best_of_three.score
            row.update(
                honest_score=score.honest_score,
                sr_oos=score.sr_oos,
                se_sr=score.se_sr,
                z_multiplier=score.z_multiplier,
                trials_haircut=score.trials_haircut,
                n_trials_used=score.n_trials,
                score_train_1y=self.best_of_three.windows.get(1, None) and self.best_of_three.windows[1].score.honest_score,
                score_train_2y=self.best_of_three.windows.get(2, None) and self.best_of_three.windows[2].score.honest_score,
                score_train_3y=self.best_of_three.windows.get(3, None) and self.best_of_three.windows[3].score.honest_score,
                winning_train_years=self.best_of_three.winning_train_years,
                train_window_spread=self.best_of_three.train_window_spread,
                oos_skew=score.skew,
                oos_kurtosis=score.kurtosis,
                oos_n_obs=score.n,
                wf_efficiency=self.best_of_three.winning_window.wf_efficiency,
                folds_profitable=self.best_of_three.winning_window.folds_profitable,
                fold_metrics=self.best_of_three.winning_window.fold_metrics,
                tuned_params_per_fold=self.best_of_three.winning_window.tuned_params_per_fold,
                n_folds=len(self.best_of_three.winning_window.folds),
            )

        if self.metrics is not None:
            row.update(self.metrics.row())

        if self.robustness is not None:
            row.update(
                deflated_sharpe=self.robustness.deflated_sharpe,
                mc_p5_return=self.robustness.monte_carlo.p5_return,
                mc_p50_return=self.robustness.monte_carlo.p50_return,
                mc_p95_return=self.robustness.monte_carlo.p95_return,
                mc_ruin_probability=self.robustness.monte_carlo.ruin_probability,
                pbo=self.robustness.pbo,
                white_rc_pvalue=self.robustness.white_rc_pvalue,
                param_sensitivity_score=self.robustness.param_sensitivity_score,
                cost_breakeven_multiplier=self.robustness.cost_breakeven_multiplier,
            )

        row.update(
            equity_curve_path=self.equity_curve_path,
            tradebook_path=self.tradebook_path,
            duration_seconds=self.duration_seconds,
            timed_out=self.timed_out,
        )
        # Provenance is NOT duplicated here: `experiments` carries those nine
        # columns (Backend-Schema §5), and `persist_evaluation` writes them
        # there. Storing them a second time on `evaluations` would give a
        # single experiment two sources of truth for "which engine scored
        # this?" that could disagree after a partial write.
        return row

    def to_dict(self) -> dict[str, Any]:
        """The structured JSON report — everything, for tooling."""
        return {
            "provenance": self.provenance.row(),
            "phase_reached": self.phase_reached,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "evaluation": self.evaluation_row(),
            "checks": [check.row() for check in self.all_checks],
            "regimes": [_regime_row(r) for r in (self.robustness.regimes if self.robustness else [])],
        }

    def to_markdown(self) -> str:
        """The human-readable rendering of the same report."""
        lines = [f"# Evaluation report — {self.outcome.upper()} at {self.phase_reached}", ""]

        if self.failure_reason:
            lines.append(f"**Failure reason:** `{self.failure_reason}`")
            lines.append("")

        score = self.honest_score
        if score is not None:
            lines += [
                "## Honest score",
                f"- `honest_score` = **{score.honest_score:.4f}**",
                f"- SR (oos) = {score.sr_oos:.4f}, SE(SR) = {score.se_sr:.4f}, "
                f"trials haircut = {score.trials_haircut:.4f}, N_trials = {score.n_trials}",
                "",
            ]

        if self.bar_verdict is not None:
            lines.append("## Bar")
            lines.append(f"- passed: **{self.bar_verdict.passed}**" + (
                f" (failed on `{self.bar_verdict.failed_on}`)" if not self.bar_verdict.passed else ""
            ))
            for check in self.bar_verdict.checks:
                lines.append(
                    f"  - `{check.test_name}`: {check.result} "
                    f"(value={check.value}, threshold={check.threshold})"
                )
            lines.append("")

        if self.metrics is not None:
            lines.append("## Core metrics")
            for key, value in self.metrics.row().items():
                lines.append(f"- {key}: {value}")
            lines.append("")

        if self.robustness is not None:
            lines += [
                "## Robustness",
                f"- deflated Sharpe (PSR): {self.robustness.deflated_sharpe:.4f}",
                f"- White's RC p-value: {self.robustness.white_rc_pvalue}",
                f"- PBO: {self.robustness.pbo}",
                f"- cost breakeven multiplier: {self.robustness.cost_breakeven_multiplier}",
                f"- Monte Carlo p5/p50/p95: {self.robustness.monte_carlo.p5_return:.4f} / "
                f"{self.robustness.monte_carlo.p50_return:.4f} / {self.robustness.monte_carlo.p95_return:.4f}",
                "",
            ]
            if self.robustness.regimes:
                lines.append("### Regime performance")
                for r in self.robustness.regimes:
                    lines.append(f"- {r.regime}: sharpe={r.sharpe:.3f}, max_dd={r.max_drawdown:.3f}, n_bars={r.n_bars}")
                lines.append("")

        if self.all_checks:
            lines.append("## All checks")
            for check in self.all_checks:
                lines.append(f"- [{check.result.upper()}] `{check.test_name}` ({check.category})")
            lines.append("")

        lines.append("## Provenance")
        for key, value in self.provenance.row().items():
            lines.append(f"- {key}: {value}")

        return "\n".join(lines)


def _regime_row(regime: RegimeSlice) -> dict[str, Any]:
    return {
        "regime": regime.regime,
        "sharpe": regime.sharpe,
        "cagr": regime.cagr,
        "max_drawdown": regime.max_drawdown,
        "trade_count": regime.trade_count,
        "period_start": regime.period_start,
        "period_end": regime.period_end,
    }

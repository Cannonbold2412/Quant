"""`evaluate.py`, productionised — the ordered funnel (App-Flow §5, TRD §10).

```
resolve profiles + provenance
        |
   complexity (bar, pre-registered)  --- FAIL --> discard, no compute spent
        |
   P0  smoke & correctness            --- FAIL --> code_error / look_ahead_detected
        |
   P1  fast backtest (frictionless)   --- FAIL --> no_signal
        |
   P2  full backtest (2x costs)       --- FAIL --> negative_expectancy / costs_exceed_edge
        |
   P3  robustness battery (walk-forward x3, MC, PBO, RC, regimes, market gates)
        |
   bar: min_trades / max_dd / cost_stress / breadth   --- FAIL --> discard, NO SCORE STORED
        |
   min_honest_score                    --- FAIL --> deflated_sharpe_insufficient
        |
   PASS -> keep, route to promotion
```

A failure at any phase **short-circuits everything after it** (App-Flow §5.2:
*"most ideas die at the bar or in P0/P1 where they cost seconds, not in P3
where they cost hours"*). `evaluate_experiment` is the one function that owns
this ordering; every phase's own module stays ignorant of what came before or
after it.

**Two things this module deliberately keeps simple, named rather than hidden:**

* **Breadth and the market gates are measured on the full-history P2
  backtest**, not fold-by-fold inside the walk-forward. The walk-forward's
  `SliceEvaluator` contract collapses a panel to one portfolio series per
  window (`evaluator.py`), which is what the honest score needs; breadth needs
  the per-instrument breakdown that only the full-history run still has. This
  is a stated approximation, not an oversight.
* **P1's "small slice" is the trailing quarter of the panel.** Cheap, recent,
  frictionless — enough to answer "is there any signal at all?" before P2 pays
  for the full history at realistic costs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ..operators.compile import CompiledSpec, compile_spec
from ..operators.registry import operator_library_version
from ..operators.spec import StrategySpec
from ..profiles.models import ResolvedProfile
from .backtest import run_backtest
from .bar import (
    BAR_FAILURE_TO_EXPERIMENT_REASON,
    AcceptanceBar,
    breadth_of,
    check_complexity,
    check_min_score,
    check_outcome,
)
from .checks import CheckResult
from .determinism import derive_seed
from .evaluator import PanelEvaluator
from .gates import GateContext, run_market_gates
from .metrics import compute_metrics
from .p0 import run_p0
from .panel import PricePanel
from .report import EvaluationReport, ProvenanceStamp, wf_config_hash
from .robustness import run_robustness
from .stats.deflated import family_trial_count
from .tradebook import extract_trades
from .version import engine_version
from .walk_forward import run_best_of_three

__all__ = ["EvaluationInputs", "evaluate_experiment"]

#: The fraction of the panel P1 checks — recent history, cheap and fast.
P1_SLICE_FRACTION = 0.25
#: P1's bar for "any signal at all": at least this many trades on the slice.
P1_MIN_TRADES = 5


@dataclass(frozen=True)
class EvaluationInputs:
    """Everything one `EVALUATE` job needs, gathered by the caller.

    Deliberately explicit rather than reaching into the database itself —
    `engine.py` is pure computation over what it is handed, which is what makes
    it testable without a live schema and safe to run inside a worker process.
    """

    spec: StrategySpec
    panel: PricePanel
    resolved: ResolvedProfile
    snapshot: dict
    acceptance_bar: AcceptanceBar
    family_prior_trials: int
    code_commit: str | None = None
    data_snapshot_id: int | None = None
    random_seed: int = 0
    cost_multiplier: float = 2.0
    param_grid: dict[str, list] | None = None
    replications: int = 500
    survivorship_handled: bool = False
    workers: int = 1


def _provenance(inputs: EvaluationInputs) -> ProvenanceStamp:
    timeframe = inputs.resolved.timeframe
    return ProvenanceStamp(
        eval_engine_version=engine_version(),
        market_profile_hash=inputs.resolved.market_profile_hash,
        timeframe_profile_hash=inputs.resolved.timeframe_profile_hash,
        cost_model_hash=inputs.resolved.cost_model_hash,
        wf_config_hash=wf_config_hash("rolling", list(timeframe.walk_forward.train_years), timeframe.walk_forward.test_years),
        operator_library_version=operator_library_version(),
        code_commit=inputs.code_commit,
        data_snapshot_id=inputs.data_snapshot_id,
        random_seed=inputs.random_seed,
    )


def _failed(
    provenance: ProvenanceStamp,
    phase: str,
    reason: str,
    checks: list[CheckResult],
    duration: float,
) -> EvaluationReport:
    return EvaluationReport(
        provenance=provenance,
        phase_reached=phase,
        outcome="failed",
        failure_reason=reason,
        bar_verdict=None,
        best_of_three=None,
        metrics=None,
        robustness=None,
        all_checks=checks,
        duration_seconds=duration,
    )


def evaluate_experiment(inputs: EvaluationInputs) -> EvaluationReport:
    """Run the full funnel once and return the report. No side effects —
    `persistence.py` is the caller's job, so this stays trivially parallel."""
    start_time = time.monotonic()
    provenance = _provenance(inputs)
    checks: list[CheckResult] = []

    # -- the bar, complexity half: before any compute is spent ------------------
    complexity_check = check_complexity(inputs.spec, inputs.acceptance_bar)
    checks.append(complexity_check)
    if complexity_check.blocking:
        return _failed(provenance, "bar", "plateaued_below_bar", checks, _elapsed(start_time))

    compiled = compile_spec(inputs.spec, inputs.resolved)

    # -- P0: smoke, correctness, look-ahead, leakage, provenance -----------------
    columns_by_instrument = [
        (instrument, inputs.panel.instrument_columns(index))
        for index, instrument in enumerate(inputs.panel.instruments)
    ]
    signal_fn = lambda columns: compiled.signals_array(columns)  # noqa: E731
    p0_checks = run_p0(
        inputs.spec, signal_fn, columns_by_instrument, inputs.snapshot, inputs.resolved.market
    )
    checks.extend(p0_checks)
    if any(check.blocking for check in p0_checks):
        # `experiments.failure_reason` has no dedicated "data provenance
        # rejected" value (Backend-Schema §6) — a snapshot that failed
        # adjustment, vault or point-in-time checks falls into `code_error`,
        # the closest available bucket, alongside genuine strategy bugs. Both
        # are non-findings routed away from the research memory either way
        # (TRD §10.1), so the coarser bucket does not corrupt anything
        # downstream; it is simply less specific than the schema could be.
        reason = "look_ahead_detected" if any(
            "lookahead" in c.test_name or "truncation" in c.test_name for c in p0_checks if c.blocking
        ) else "code_error"
        return _failed(provenance, "P0", reason, checks, _elapsed(start_time))

    # -- P1: fast, frictionless, small slice — is there any signal at all? ------
    slice_start_index = int(inputs.panel.n_bars * (1.0 - P1_SLICE_FRACTION))
    p1_panel = inputs.panel.slice_bars(slice_start_index, inputs.panel.n_bars)
    p1_signals = _full_signals(compiled, p1_panel)
    p1_result = run_backtest(p1_panel, p1_signals, inputs.resolved, cost_multiplier=0.0)
    p1_check = CheckResult(
        "p1_any_signal",
        "performance",
        "pass" if p1_result.n_trades >= P1_MIN_TRADES else "fail",
        value=float(p1_result.n_trades),
        threshold=float(P1_MIN_TRADES),
    )
    checks.append(p1_check)
    if p1_check.blocking:
        return _failed(provenance, "P1", "no_signal", checks, _elapsed(start_time))

    # -- P2: full history, realistic costs — a real, survivable edge? ----------
    full_signals = _full_signals(compiled, inputs.panel)
    gross_full = run_backtest(inputs.panel, full_signals, inputs.resolved, cost_multiplier=0.0)
    net_full = run_backtest(inputs.panel, full_signals, inputs.resolved, cost_multiplier=inputs.cost_multiplier)

    checks.append(
        CheckResult(
            "p2_gross_expectancy",
            "performance",
            "pass" if gross_full.portfolio_returns.sum() > 0.0 else "fail",
            value=float(gross_full.portfolio_returns.sum()),
            threshold=0.0,
        )
    )
    if gross_full.portfolio_returns.sum() <= 0.0:
        return _failed(provenance, "P2", "negative_expectancy", checks, _elapsed(start_time))

    checks.append(
        CheckResult(
            "p2_cost_survival",
            "cost",
            "pass" if net_full.portfolio_returns.sum() > 0.0 else "fail",
            value=float(net_full.portfolio_returns.sum()),
            threshold=0.0,
        )
    )
    if net_full.portfolio_returns.sum() <= 0.0:
        return _failed(provenance, "P2", "costs_exceed_edge", checks, _elapsed(start_time))

    # -- P3: the robustness battery ----------------------------------------------
    evaluator = PanelEvaluator(inputs.panel, compiled, inputs.resolved, inputs.cost_multiplier)
    holding_period = _estimate_holding_period(net_full)
    n_trials = family_trial_count(inputs.family_prior_trials, iterations=1)

    best_of_three = run_best_of_three(
        inputs.panel.dates,
        evaluator,
        {},
        holding_period_bars=holding_period,
        n_trials=n_trials,
        periods_per_year=inputs.resolved.periods_per_year,
        param_grid=inputs.param_grid,
        base_seed=inputs.random_seed,
        workers=inputs.workers,
    )
    window = best_of_three.winning_window

    robustness = run_robustness(
        window,
        inputs.panel,
        inputs.resolved.periods_per_year,
        n_trials,
        replications=inputs.replications,
        base_seed=derive_seed(inputs.random_seed, "robustness"),
    )

    # -- the bar, data-dependent half ---------------------------------------------
    breadth = breadth_of(net_full.instrument_pnl, net_full.instrument_trades)
    bar_verdict = check_outcome(
        inputs.acceptance_bar.for_market(inputs.resolved.market),
        n_trades=window.total_trades,
        max_drawdown=_max_drawdown(window.concatenated_returns),
        oos_return_total=float(window.concatenated_returns.sum()),
        breadth=breadth,
    )
    checks.extend(bar_verdict.checks)

    gate_context = GateContext(
        panel=inputs.panel,
        result=net_full,
        resolved=inputs.resolved,
        survivorship_handled=inputs.survivorship_handled,
    )
    market_checks = run_market_gates(gate_context)
    checks.extend(market_checks)

    if bar_verdict.blocks_scoring:
        # Not the generic `_failed()` — that helper hardcodes `bar_verdict=None`,
        # which is correct for every earlier phase (there is no bar_verdict yet)
        # but wrong here: the four-item bar was just computed, and
        # `handlers/evaluate.py`'s PROMOTE/REVIEW routing (App-Flow §6.1) reads
        # `report.bar_verdict` directly. Losing it here would silently make a
        # bar failure indistinguishable from a P0-P2 failure and never enqueue
        # `REVIEW` at all. `failure_reason` is translated through
        # `BAR_FAILURE_TO_EXPERIMENT_REASON` — `bar_verdict.failed_on` is this
        # module's own vocabulary (and exactly what `evaluations.bar_failed_on`
        # stores), not `experiments.failure_reason`'s.
        return EvaluationReport(
            provenance=provenance,
            phase_reached="P3",
            outcome="failed",
            failure_reason=BAR_FAILURE_TO_EXPERIMENT_REASON[bar_verdict.failed_on],
            bar_verdict=bar_verdict,
            best_of_three=best_of_three,
            metrics=None,
            robustness=robustness,
            all_checks=checks,
            duration_seconds=_elapsed(start_time),
        )

    score_check = check_min_score(inputs.acceptance_bar, window.score.honest_score)
    checks.append(score_check)
    if score_check.result == "fail":
        return EvaluationReport(
            provenance=provenance,
            phase_reached="P3",
            outcome="failed",
            failure_reason="deflated_sharpe_insufficient",
            bar_verdict=bar_verdict,
            best_of_three=best_of_three,
            metrics=None,
            robustness=robustness,
            all_checks=checks,
            duration_seconds=_elapsed(start_time),
        )

    metrics = compute_metrics(
        net_full, extract_trades(net_full), inputs.resolved.periods_per_year
    )

    return EvaluationReport(
        provenance=provenance,
        phase_reached="P3",
        outcome="passed",
        failure_reason=None,
        bar_verdict=bar_verdict,
        best_of_three=best_of_three,
        metrics=metrics,
        robustness=robustness,
        all_checks=checks,
        duration_seconds=_elapsed(start_time),
    )


def _full_signals(compiled: CompiledSpec, panel: PricePanel) -> np.ndarray:
    return np.column_stack(
        [
            compiled.signals_array(panel.instrument_columns(index))
            for index in range(panel.n_instruments)
        ]
    )


def _estimate_holding_period(result) -> int:
    """A coarse pre-check, purely to size the walk-forward embargo (TRD §8.4).

    The embargo must be at least the holding period or trades straddle the
    train/test boundary and leak; this is a rough estimate from the
    frictionless full-history run, not a claim about the strategy's true
    average hold.
    """
    if result.n_trades == 0:
        return 5
    estimate = result.n_bars / result.n_trades
    return int(np.clip(estimate, 1, 60))


def _max_drawdown(returns: np.ndarray) -> float:
    from .backtest import max_drawdown_from_returns

    return max_drawdown_from_returns(returns)


def _elapsed(start_time: float) -> float:
    return time.monotonic() - start_time

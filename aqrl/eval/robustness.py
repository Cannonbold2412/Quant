"""P3 — the robustness battery, orchestrated (TRD §10, App-Flow §5).

Everything below runs on the **winning window's concatenated out-of-sample
series** — the same series the honest score is computed from, never the fitted
sample. That is deliberate: robustness statistics computed on in-sample data are
not robustness statistics, they are curve-fit decoration.

Five things happen here, in the order the funnel names them:

1. **Deflated Sharpe** — a diagnostic re-statement of the same haircut as a
   probability, since a probability saturates and cannot drive the score itself
   (TRD §7.3) but is a readable number for a report.
2. **Monte Carlo** — the distribution of outcomes a block bootstrap of this
   exact series could plausibly have produced.
3. **White's Reality Check / CSCV·PBO** — both need a *family* of candidate
   series, which the walk-forward's tuning grid already produced as a side
   effect (`WindowResult.grid_returns`). With no grid (no tuning), there is
   nothing to have overfitted by selection, so both report their "nothing to
   see" value honestly rather than being skipped silently.
4. **Regime analysis** — labelled from the *market*, never the strategy's own
   P&L (`regimes.py`'s docstring explains why that would be circular).
5. **Cost sensitivity** — where the edge actually dies as costs rise, found by
   re-running the backtest at each multiplier in `TimeframeProfile.cost_stress_multipliers`
   plus a widening sweep past it. This reuses `WindowResult.concatenated_gross`
   and `.concatenated_costs`, which are pre-multiplier and per-1× respectively
   — see `costs.py`'s linearity: total cost scales exactly with the multiplier,
   so re-pricing needs no new backtest at all.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .determinism import derive_seed
from .panel import PricePanel
from .regimes import RegimeSlice, label_regimes, market_returns, regime_performance
from .stats.cscv import probability_of_backtest_overfitting
from .stats.deflated import deflated_sharpe_ratio
from .stats.monte_carlo import MonteCarloResult, monte_carlo_paths
from .stats.reality_check import whites_reality_check
from .walk_forward import WindowResult

__all__ = ["RobustnessResult", "cost_breakeven_multiplier", "run_robustness"]


@dataclass(frozen=True)
class RobustnessResult:
    deflated_sharpe: float
    monte_carlo: MonteCarloResult
    white_rc_pvalue: float | None
    pbo: float | None
    param_sensitivity_score: float | None
    cost_breakeven_multiplier: float | None
    regimes: list[RegimeSlice]


def cost_breakeven_multiplier(
    gross_returns: np.ndarray,
    costs_at_1x: np.ndarray,
    max_multiplier: float = 10.0,
    step: float = 0.1,
) -> float | None:
    """The largest cost multiplier the concatenated series still survives.

    Costs scale linearly in the multiplier (`costs.py`), so re-pricing at a new
    multiplier is `gross − multiplier × costs_at_1x` — no new backtest needed.
    `None` means it does not survive even the reported multiplier, which the
    bar's `cost_stress` item will already have failed on.
    """
    gross_returns = np.asarray(gross_returns, dtype=float)
    costs_at_1x = np.asarray(costs_at_1x, dtype=float)
    if gross_returns.size == 0:
        return None

    best: float | None = None
    multiplier = step
    while multiplier <= max_multiplier:
        net = gross_returns - multiplier * costs_at_1x
        if net.sum() <= 0.0:
            break
        best = multiplier
        multiplier += step
    return best


def param_sensitivity(grid_returns: np.ndarray | None, periods_per_year: float) -> float | None:
    """How much the score moves across the tuning grid — high is fragile.

    Reported as the coefficient of variation of each configuration's Sharpe:
    low means the strategy's edge is not hanging off one lucky parameter draw.
    `None`, honestly, when there was no grid to measure dispersion over.
    """
    if grid_returns is None or grid_returns.shape[1] < 2:
        return None
    from .stats.honest_score import sharpe_ratio

    sharpes = np.array(
        [sharpe_ratio(grid_returns[:, column], periods_per_year) for column in range(grid_returns.shape[1])]
    )
    mean = float(np.abs(sharpes).mean())
    if mean == 0.0:
        return None
    return float(sharpes.std(ddof=1) / mean)


def run_robustness(
    window: WindowResult,
    panel: PricePanel,
    periods_per_year: float,
    n_trials: int,
    replications: int = 500,
    base_seed: int = 0,
) -> RobustnessResult:
    """The full P3 battery for one (winning) walk-forward window."""
    returns = window.concatenated_returns

    deflated = deflated_sharpe_ratio(returns, periods_per_year, n_trials)
    monte_carlo = monte_carlo_paths(
        returns, replications=replications, base_seed=derive_seed(base_seed, "mc")
    )

    if window.grid_returns is not None:
        white_rc = whites_reality_check(
            [window.grid_returns[:, column] for column in range(window.grid_returns.shape[1])],
            replications=replications,
            base_seed=derive_seed(base_seed, "rc"),
        )
        pbo = probability_of_backtest_overfitting(window.grid_returns)
    else:
        white_rc, pbo = None, None

    sensitivity = param_sensitivity(window.grid_returns, periods_per_year)

    if window.concatenated_gross is not None and window.concatenated_costs is not None:
        breakeven = cost_breakeven_multiplier(window.concatenated_gross, window.concatenated_costs)
    else:
        breakeven = None

    market = market_returns(panel)
    labels = label_regimes(market)
    # The concatenated series has embargo gaps removed and folds joined, so it
    # is not the same length or timeline as the panel. Regime labels are
    # matched back to it by date, since that is the one thing both series
    # genuinely share.
    date_index = {np.datetime64(date, "D"): position for position, date in enumerate(panel.dates)}
    aligned_labels = np.array(
        [
            labels[date_index[np.datetime64(date, "D")]]
            if np.datetime64(date, "D") in date_index
            else "sideways"
            for date in window.concatenated_dates
        ],
        dtype=object,
    )
    regimes = regime_performance(returns, window.concatenated_dates, aligned_labels, periods_per_year)

    return RobustnessResult(
        deflated_sharpe=deflated,
        monte_carlo=monte_carlo,
        white_rc_pvalue=white_rc,
        pbo=pbo,
        param_sensitivity_score=sensitivity,
        cost_breakeven_multiplier=breakeven,
        regimes=regimes,
    )

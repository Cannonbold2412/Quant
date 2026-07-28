"""The honest score — TRD §7.

    score = SR_oos - z * SE(SR) - E[max SR | N_trials]

A single float: the deflated lower bound on out-of-sample Sharpe. It is the
only number that drives keep/discard (README, TRD §7.1). Every other metric
computed elsewhere in the pipeline is diagnostic only.

Known simplification (TRD §7.7): no autocorrelation correction is applied to
`n` yet. Frequency fairness across wildly different trade frequencies is an
open item in the docs, not solved here.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True)
class HonestScoreResult:
    honest_score: float
    sr_oos: float
    se_sr: float
    trials_haircut: float
    n: int
    skew: float
    kurtosis: float
    z_multiplier: float
    n_trials: int


def sharpe_ratio(returns: np.ndarray, periods_per_year: float) -> float:
    """Annualised Sharpe ratio. `periods_per_year` always comes from a
    TimeframeProfile — never hardcoded (TRD §1 Stage 1 requirement, applied
    here even though nanoAQRL has no formal profile object yet)."""
    returns = np.asarray(returns, dtype=float)
    if returns.size == 0:
        return 0.0
    std = returns.std(ddof=1)
    if std == 0.0:
        return 0.0
    return float(returns.mean() / std * np.sqrt(periods_per_year))


def se_sharpe(sr: float, skew: float, kurtosis: float, n: int) -> float:
    """TRD §7.2, term 2:

        SE(SR) = sqrt( (1 + SR^2/2 - skew*SR + (kurtosis-3)/4 * SR^2) / n )

    `kurtosis` here is the non-excess (Pearson) kurtosis, i.e. 3.0 for a
    normal distribution — matching the formula's `(kurtosis - 3)` term.
    """
    if n <= 1:
        return float("inf")
    inner = 1.0 + (sr**2) / 2.0 - skew * sr + (kurtosis - 3.0) / 4.0 * sr**2
    inner = max(inner, 0.0)
    return float(np.sqrt(inner / n))


def expected_max_sharpe_under_null(se_sr_for_trials: float, n_trials: int) -> float:
    """The trials haircut, TRD §7.2 term 3 (Bailey & Lopez de Prado).

    Expected value of the *best* Sharpe ratio observed across `n_trials`
    independent trials on pure noise:

        E[max_N SR] = sigma_SR * ( (1-gamma)*Z^-1(1 - 1/N) + gamma*Z^-1(1 - 1/(N*e)) )

    The formula is asymptotic in N; at N<=1 there is no selection, so the
    haircut is defined as 0 rather than evaluating the (undefined) N=1 case.
    """
    if n_trials <= 1 or not np.isfinite(se_sr_for_trials):
        return 0.0
    n = float(n_trials)
    term1 = (1.0 - EULER_MASCHERONI) * stats.norm.ppf(1.0 - 1.0 / n)
    term2 = EULER_MASCHERONI * stats.norm.ppf(1.0 - 1.0 / (n * np.e))
    return float(se_sr_for_trials * (term1 + term2))


def compute_honest_score(
    returns: np.ndarray,
    periods_per_year: float,
    n_trials: int,
    z_multiplier: float = 1.65,
) -> HonestScoreResult:
    """Score a single concatenated OOS return series (TRD §8.3: always
    concatenated, never averaged per-fold)."""
    returns = np.asarray(returns, dtype=float)
    n = int(returns.size)
    sr = sharpe_ratio(returns, periods_per_year)

    if n > 3:
        skew = float(stats.skew(returns, bias=False))
        kurt = float(stats.kurtosis(returns, fisher=False, bias=False))
    else:
        skew, kurt = 0.0, 3.0

    se = se_sharpe(sr, skew, kurt, n)
    haircut = expected_max_sharpe_under_null(se, n_trials)
    score = sr - z_multiplier * se - haircut

    return HonestScoreResult(
        honest_score=float(score),
        sr_oos=sr,
        se_sr=se,
        trials_haircut=haircut,
        n=n,
        skew=skew,
        kurtosis=kurt,
        z_multiplier=z_multiplier,
        n_trials=n_trials,
    )

"""The honest score — TRD §7.

    score = SR_oos - z * SE(SR) - E[max SR | N_trials]

A single float: the deflated lower bound on out-of-sample Sharpe. It is the
only number that drives keep/discard (README, TRD §7.1). Every other metric
computed elsewhere in the pipeline is diagnostic only.

**This is the only implementation.** It moved here from `nanoaqrl/_lib/` at
Stage 3 rather than being reimplemented beside it, because TRD §6.1 makes that
a scientific requirement and not a style preference: A5 compares experiment
#6,201 in crypto against #12,483 in Indian equities, and that comparison is
meaningless unless both were scored by identical code. Two copies of this file
would diverge silently, and a Sharpe of 1.4 would stop meaning the same thing in
two rows of the same table. The research loop imports these names from here
directly; the former re-export shim is gone with the `nanoaqrl/` package.

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

    # A (near-)zero-variance series (no trades, or a perfectly flat signal) has
    # no meaningfully defined skew or kurtosis — scipy's higher moments lose all
    # precision to cancellation as the values converge and can return NaN,
    # which would otherwise poison the whole score. The normal-distribution
    # values are the honest fallback: SE(SR) collapses to the plain 1/sqrt(n)
    # case, matching what the formula intends when there is no higher-moment
    # information to speak of. The threshold is relative to the series' own
    # scale, since an absolute one would misjudge a strategy trading in basis
    # points rather than percent. Guarded behind `n > 3` first, so an empty or
    # near-empty series (no folds survived, or none were profitable) never
    # reaches `std`/`mean` on nothing and raises a RuntimeWarning for a value
    # that would be discarded anyway.
    if n > 3:
        dispersion = returns.std(ddof=1)
        scale = np.abs(returns).mean()
    else:
        dispersion = scale = 0.0
    if n > 3 and dispersion > 1e-9 * max(scale, 1.0):
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

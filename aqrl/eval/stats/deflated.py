"""Deflated Sharpe and the trial count that makes it honest.

The deflated Sharpe asks: *given that we tried `N` things, how likely is this
Sharpe under the null that none of them had an edge?* It is only as honest as
`N`, and **under-counting is the easiest way to make the whole score
dishonest** (TRD §7.2).

What counts, from TRD §8.6:

| Activity | Selects on | Counts? |
|---|---|---|
| Parameter tuning inside a fold, on training data | train performance | ❌ never saw the test window |
| Choosing the best of 3 train windows by OOS score | the reported score | ✅ **×3** |
| Each A2↔A3 iteration, changed after seeing the OOS result | the reported score | ✅ +1 each |
| Prior experiments in the same family, including the same idea in another market | the reported score | ✅ |

The principle: *if a human or agent looked at an out-of-sample number and then
changed something, that is a trial.* Trying the same idea across three markets
is three trials, not one — which is why `strategies.family` exists and why the
count is an indexed query rather than a guess (TRD §10.2).
"""
from __future__ import annotations

import numpy as np
from scipy import stats

from .honest_score import expected_max_sharpe_under_null, se_sharpe, sharpe_ratio

__all__ = ["deflated_sharpe_ratio", "family_trial_count"]

#: Every strategy is evaluated at three train-window lengths and the best is
#: reported. That selection is invisible to the haircut unless it is counted.
TRAIN_WINDOW_MULTIPLIER = 3


def family_trial_count(prior_trials: int, iterations: int = 1) -> int:
    """`(prior family trials + this attempt) × 3` — the count fed to the haircut.

    The ×3 is the honest price of best-of-three (TRD §8.2): the haircut is three
    times larger than under a single fixed window, and the bar is that much
    harder to clear. That is the intended consequence, not an accident to be
    tuned away.
    """
    return max(prior_trials + iterations, 1) * TRAIN_WINDOW_MULTIPLIER


def deflated_sharpe_ratio(
    returns: np.ndarray,
    periods_per_year: float,
    n_trials: int,
) -> float:
    """Probability that the observed Sharpe exceeds the best expected under the null.

    Bailey & López de Prado's PSR evaluated against `SR*(N)` rather than zero.
    Reported as a **diagnostic** — the keep/discard decision is the honest score
    (TRD §7.3: a probability saturates, and two good strategies both scoring
    0.99 leave the hill-climb with no gradient).
    """
    returns = np.asarray(returns, dtype=float)
    n = returns.size
    if n < 4:
        return 0.0

    sr = sharpe_ratio(returns, periods_per_year)
    skew = float(stats.skew(returns, bias=False))
    kurtosis = float(stats.kurtosis(returns, fisher=False, bias=False))
    standard_error = se_sharpe(sr, skew, kurtosis, n)
    if not np.isfinite(standard_error) or standard_error <= 0.0:
        return 0.0

    threshold = expected_max_sharpe_under_null(standard_error, n_trials)
    return float(stats.norm.cdf((sr - threshold) / standard_error))

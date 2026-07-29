"""The statistical layer — market-agnostic mathematics, existing exactly once.

Deflated Sharpe, White's Reality Check, CSCV/PBO, Monte Carlo and the honest
score are the same computations whether the instrument is a NIFTY-50 name or a
crypto perpetual. Forking them per market is forbidden (TRD §6.1): within a year
you have six subtly divergent PBO implementations, a Sharpe of 1.4 no longer
means the same thing in two rows of the same table, and every cross-market
lesson is noise — silently.
"""

from .cscv import probability_of_backtest_overfitting
from .deflated import deflated_sharpe_ratio, family_trial_count
from .honest_score import (
    HonestScoreResult,
    compute_honest_score,
    expected_max_sharpe_under_null,
    se_sharpe,
    sharpe_ratio,
)
from .monte_carlo import MonteCarloResult, monte_carlo_paths
from .reality_check import whites_reality_check

__all__ = [
    "HonestScoreResult",
    "MonteCarloResult",
    "compute_honest_score",
    "deflated_sharpe_ratio",
    "expected_max_sharpe_under_null",
    "family_trial_count",
    "monte_carlo_paths",
    "probability_of_backtest_overfitting",
    "se_sharpe",
    "sharpe_ratio",
    "whites_reality_check",
]

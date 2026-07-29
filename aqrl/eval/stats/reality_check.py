"""White's Reality Check — is the best of N strategies better than luck?

The deflated Sharpe answers this parametrically, from `N` and the moments of the
return series. White's test answers it non-parametrically, by bootstrapping the
whole family's returns under the null that no strategy has an edge and asking
how often the null produces a winner this good.

They are kept as two tests rather than one because they fail differently: the
deflated Sharpe leans on a normal approximation that fat tails strain, and the
Reality Check leans on the bootstrap preserving the dependence structure. A
result that survives both is better evidenced than one that survives either.

The p-value is against the **best** strategy in the family, which is the number
selection bias actually operates on.
"""
from __future__ import annotations

import numpy as np

from ..determinism import rng_for
from .monte_carlo import block_bootstrap_indices

__all__ = ["whites_reality_check"]


def whites_reality_check(
    families: list[np.ndarray],
    replications: int = 500,
    base_seed: int = 0,
    mean_block: float = 20.0,
) -> float:
    """p-value for the best mean return across `families` being real.

    Each entry is one candidate's return series. Series are re-centred on their
    own mean before resampling, which is what imposes the null — under it every
    candidate has zero expected return, and any winner is sampling noise.

    A single-candidate family is a legitimate call: it degenerates into a
    bootstrap test of one mean, which is exactly what "we only tried one thing"
    should give.
    """
    series = [np.asarray(candidate, dtype=float) for candidate in families if candidate.size > 1]
    if not series:
        return 1.0

    length = min(candidate.size for candidate in series)
    matrix = np.vstack([candidate[-length:] for candidate in series])
    scale = np.sqrt(length)
    observed = float(np.max(matrix.mean(axis=1) * scale))

    centred = matrix - matrix.mean(axis=1, keepdims=True)
    exceedances = 0
    for replication in range(replications):
        rng = rng_for(base_seed, "reality_check", replication)
        indices = block_bootstrap_indices(length, rng, mean_block)
        resampled = float(np.max(centred[:, indices].mean(axis=1) * scale))
        if resampled >= observed:
            exceedances += 1

    # The +1 in both terms is the standard finite-sample correction: with B
    # replications the smallest attainable p-value is 1/(B+1), never 0. A
    # reported p of exactly zero would claim more evidence than B draws contain.
    return float((exceedances + 1) / (replications + 1))

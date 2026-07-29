"""Monte Carlo — what else the same edge could plausibly have produced.

One equity curve is one draw. Resampling it answers the question a single
backtest cannot: *how much of this path was the edge, and how much was the order
the returns happened to arrive in?*

Two resamplers, because they answer different questions:

* **IID bootstrap** breaks all ordering. Right for "could this sequence of
  returns have produced a much worse drawdown in a different order?"
* **Stationary (block) bootstrap** preserves local structure, so a strategy that
  depends on momentum clustering is not handed a free improvement by having its
  clusters shuffled away.

The block version is the default here. Trading returns are autocorrelated —
overlapping positions and slow-decaying signals both do it (TRD §7.7) — and an
IID bootstrap of autocorrelated returns understates tail risk.

`mc_ruin_probability` is the fraction of resampled paths whose drawdown exceeds
the ruin threshold. It is a diagnostic, never a ranking input.

**Numba, applied here on profiled evidence, not by default.** Stage 2 left the
seam deliberately open, pending Stage 3's own profiling (TRD §9.6-9.7:
"correct first, then measure, then optimise the measured bottleneck"). A full
`evaluate_experiment` run profiled at ~46% of its wall time inside this
module's index-building loop — the textbook "genuinely path-dependent logic"
TRD §9.2 names as Numba's use case, since each step's position depends on
the previous one and cannot be vectorised away. `@njit` is applied to that
loop alone, not speculatively elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numba import njit

from ..determinism import rng_for

__all__ = ["MonteCarloResult", "block_bootstrap_indices", "monte_carlo_paths"]

#: A path losing this fraction from its peak is counted as ruined.
DEFAULT_RUIN_DRAWDOWN = 0.50


@dataclass(frozen=True)
class MonteCarloResult:
    p5_return: float
    p50_return: float
    p95_return: float
    ruin_probability: float
    replications: int


@njit(cache=True)
def _block_bootstrap_loop(
    n: int, start_position: int, restart: np.ndarray, jump_targets: np.ndarray
) -> np.ndarray:
    """The sequential part alone: given precomputed random draws, walk the
    stationary-bootstrap chain. No RNG calls inside the jitted loop — numba's
    nopython mode does not support `np.random.Generator`, so every draw is
    made in Python first (vectorised, and just as fast) and handed in as plain
    arrays; only the position-dependent bookkeeping — which cannot be
    vectorised, since each step depends on the previous one — is compiled.

    `restart[step]` is `True` exactly where the chain jumps to a fresh random
    block; `jump_targets[step]` is only meaningful there.
    """
    indices = np.empty(n, dtype=np.int64)
    position = start_position
    for step in range(n):
        indices[step] = position
        if restart[step]:
            position = jump_targets[step]
        else:
            position = (position + 1) % n
    return indices


def block_bootstrap_indices(n: int, rng: np.random.Generator, mean_block: float) -> np.ndarray:
    """Stationary bootstrap indices: geometric block lengths, wrapping.

    All randomness is drawn here, vectorised, from the caller's `rng` — the
    jitted loop below only walks the chain those draws define.
    """
    probability = 1.0 / max(mean_block, 1.0)
    start_position = int(rng.integers(0, n))
    restart = rng.random(n) < probability
    jump_targets = rng.integers(0, n, size=n)
    return _block_bootstrap_loop(n, start_position, restart, jump_targets)


def monte_carlo_paths(
    returns: np.ndarray,
    replications: int = 500,
    base_seed: int = 0,
    mean_block: float = 20.0,
    ruin_drawdown: float = DEFAULT_RUIN_DRAWDOWN,
) -> MonteCarloResult:
    """Resample the return series and summarise the distribution of outcomes."""
    returns = np.asarray(returns, dtype=float)
    if returns.size < 2 or replications < 1:
        return MonteCarloResult(0.0, 0.0, 0.0, 0.0, 0)

    totals = np.empty(replications)
    ruined = 0
    for replication in range(replications):
        # Seeded per replication from the base seed — never from the worker or
        # the clock, so a parallel run reproduces a serial one exactly.
        rng = rng_for(base_seed, "monte_carlo", replication)
        path = returns[block_bootstrap_indices(returns.size, rng, mean_block)]
        equity = np.cumprod(1.0 + path)
        totals[replication] = equity[-1] - 1.0
        drawdown = 1.0 - equity / np.maximum.accumulate(equity)
        if drawdown.max() >= ruin_drawdown:
            ruined += 1

    return MonteCarloResult(
        p5_return=float(np.percentile(totals, 5)),
        p50_return=float(np.percentile(totals, 50)),
        p95_return=float(np.percentile(totals, 95)),
        ruin_probability=float(ruined / replications),
        replications=replications,
    )

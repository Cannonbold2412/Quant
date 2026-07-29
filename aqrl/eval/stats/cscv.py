"""CSCV → the probability of backtest overfitting (PBO).

Bailey, Borwein, López de Prado & Zhu. The question: *when we pick the
best-performing configuration in-sample, how often is it below median
out-of-sample?* If the answer is "about half the time", the selection procedure
is learning noise — and PBO measures that directly, without needing to know
anything about the strategies themselves.

The construction: split the return series into `S` contiguous chunks, take every
way of choosing `S/2` of them as the in-sample half (the rest is out-of-sample),
pick the configuration with the best in-sample performance, and record its
out-of-sample rank. PBO is the fraction of splits where that rank is below the
median.

**Contiguous chunks, recombined.** Chunks are contiguous slices of time rather
than random bars, because shuffling bars destroys the autocorrelation that makes
a strategy overfittable in the first place.

Needs a *matrix* — one column per configuration — so it is computed over the
parameter grid's per-fold results. With a single configuration there is no
selection to be overfitted by, and it returns 0 with that stated.
"""
from __future__ import annotations

import itertools

import numpy as np

__all__ = ["probability_of_backtest_overfitting"]

#: Chunks. Must be even. 16 gives 12,870 combinations, which is the usual
#: recommendation and stays fast because each evaluation is a mean.
DEFAULT_CHUNKS = 16


def probability_of_backtest_overfitting(
    performance: np.ndarray,
    chunks: int = DEFAULT_CHUNKS,
    max_combinations: int = 4096,
) -> float:
    """PBO from an `(n_bars, n_configurations)` matrix of returns.

    Returns a probability in `[0, 1]`. High is bad: 0.5 means picking the
    in-sample winner is no better than picking at random.
    """
    performance = np.asarray(performance, dtype=float)
    if performance.ndim != 2 or performance.shape[1] < 2:
        return 0.0

    n_bars, n_configs = performance.shape
    chunks = min(chunks - chunks % 2, n_bars)
    if chunks < 4:
        return 0.0

    edges = np.array_split(np.arange(n_bars), chunks)
    half = chunks // 2

    combinations = list(itertools.combinations(range(chunks), half))
    if len(combinations) > max_combinations:
        # Deterministic thinning — evenly spaced through the enumeration, never
        # sampled, so the same matrix always yields the same PBO (TRD §9.5).
        step = len(combinations) / max_combinations
        combinations = [combinations[int(index * step)] for index in range(max_combinations)]

    logits = []
    for in_sample_chunks in combinations:
        in_sample_index = np.concatenate([edges[chunk] for chunk in in_sample_chunks])
        out_of_sample_index = np.concatenate(
            [edges[chunk] for chunk in range(chunks) if chunk not in in_sample_chunks]
        )

        in_sample = _score(performance[in_sample_index])
        out_of_sample = _score(performance[out_of_sample_index])
        best = int(np.argmax(in_sample))

        # Relative rank of the in-sample winner among out-of-sample results.
        rank = float((out_of_sample <= out_of_sample[best]).sum()) / (n_configs + 1)
        rank = min(max(rank, 1e-6), 1 - 1e-6)
        logits.append(np.log(rank / (1.0 - rank)))

    return float(np.mean(np.asarray(logits) <= 0.0))


def _score(block: np.ndarray) -> np.ndarray:
    """Per-configuration performance over a block: mean over standard deviation.

    A ratio rather than a total, so a configuration cannot win the in-sample
    half simply by being more heavily invested.
    """
    std = block.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(std > 0.0, block.mean(axis=0) / std, 0.0)

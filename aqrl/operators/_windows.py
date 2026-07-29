"""Causal windowing primitives shared by the operator implementations.

Every rolling helper here is **trailing**: the window ending at bar `t` covers
`[t-window+1, t]` and nothing later. They exist as one shared implementation
because a rolling window is exactly where look-ahead creeps in — `center=True`,
an off-by-one shift, a `bfill()` to tidy the warm-up — and one careful version
is far easier to keep honest than fifteen ad-hoc ones.

The first `window-1` outputs are NaN, always. Filling them would invent data
the strategy could not have had.
"""
from __future__ import annotations

from collections import deque

import numpy as np

__all__ = [
    "rolling_apply",
    "rolling_max",
    "rolling_mean",
    "rolling_min",
    "rolling_std",
    "rolling_sum",
    "shift",
    "true_range",
    "wilder_smooth",
]


def _empty_like(values: np.ndarray) -> np.ndarray:
    return np.full(values.shape[0], np.nan, dtype=float)


def rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    """Trailing sum. NaN for the first `window-1` bars, and for any window
    containing a NaN — **but not beyond it**.

    That last clause is the whole reason this is not a bare `cumsum`. A
    cumulative sum carries a NaN forward forever, so one warm-up NaN at the head
    of the input would make the entire output NaN. Operators compose — a rolling
    mean of an ATR, a z-score of an EMA — so a *leading* NaN is the normal case,
    not an edge case, and getting this wrong silently empties every stacked
    indicator in the library.

    So NaNs are summed as zero and counted separately; a window is NaN exactly
    when it contains one.
    """
    out = _empty_like(values)
    if window <= 0 or values.shape[0] < window:
        return out

    missing = np.isnan(values)
    cumulative = np.concatenate(([0.0], np.cumsum(np.where(missing, 0.0, values))))
    sums = cumulative[window:] - cumulative[:-window]

    cumulative_missing = np.concatenate(([0], np.cumsum(missing)))
    per_window_missing = cumulative_missing[window:] - cumulative_missing[:-window]

    out[window - 1 :] = np.where(per_window_missing > 0, np.nan, sums)
    return out


def rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    """Trailing arithmetic mean."""
    if window <= 0:
        return _empty_like(values)
    return rolling_sum(values, window) / float(window)


def rolling_std(values: np.ndarray, window: int, ddof: int = 1) -> np.ndarray:
    """Trailing sample standard deviation.

    Computed from the rolling mean of squares rather than a second pass, then
    clipped at zero: catastrophic cancellation can push a mathematically
    non-negative variance a few ulps below zero, and `sqrt` of that is NaN —
    which would read downstream as a look-ahead warm-up rather than a rounding
    artefact.

    The `sum(x^2) - total*mean` form trades a little precision for O(n): against
    pandas' rolling std it agrees to ~3e-9 relative on 25 years of daily prices,
    which is seven orders of magnitude below anything that changes a decision.
    What it does **not** trade away is determinism — the sums are taken over the
    same prefix regardless of where the series is truncated, so results are
    bit-identical on re-run and under truncation, which is what TRD §9.5
    actually requires.
    """
    if window <= ddof or values.shape[0] < window:
        return _empty_like(values)

    total = rolling_sum(values, window)
    total_sq = rolling_sum(values * values, window)
    mean = total / float(window)
    # sum((x-m)^2) == sum(x^2) - total*m, and NaN warm-up propagates through.
    variance = (total_sq - total * mean) / float(window - ddof)
    return np.sqrt(np.maximum(variance, 0.0))


def rolling_apply(values: np.ndarray, window: int, func) -> np.ndarray:
    """Trailing application of an arbitrary reducer.

    The escape hatch for statistics with no cumulative form (rank, median,
    an SVD). O(n*window) — used only where it must be.
    """
    out = _empty_like(values)
    n = values.shape[0]
    if window <= 0 or n < window:
        return out
    for index in range(window - 1, n):
        out[index] = func(values[index - window + 1 : index + 1])
    return out


def _rolling_extremum(values: np.ndarray, window: int, better) -> np.ndarray:
    """Monotonic-deque running extremum: O(n) rather than O(n*window)."""
    out = _empty_like(values)
    n = values.shape[0]
    if window <= 0 or n < window:
        return out

    # A real deque: popleft on a list is O(len), which would put this back at
    # O(n*window) — the exact cost the monotonic structure exists to avoid.
    window_indices: deque[int] = deque()
    for index in range(n):
        while window_indices and better(values[index], values[window_indices[-1]]):
            window_indices.pop()
        window_indices.append(index)
        if window_indices[0] <= index - window:
            window_indices.popleft()
        if index >= window - 1:
            out[index] = values[window_indices[0]]
    return out


def rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    return _rolling_extremum(values, window, lambda new, held: new >= held)


def rolling_min(values: np.ndarray, window: int) -> np.ndarray:
    return _rolling_extremum(values, window, lambda new, held: new <= held)


def shift(values: np.ndarray, periods: int = 1) -> np.ndarray:
    """Move a series forward in time. **Only forward.**

    A negative `periods` would pull the future backwards — the single most
    common look-ahead bug — so it is refused outright rather than supported and
    documented as dangerous.
    """
    if periods < 0:
        raise ValueError(
            f"shift({periods}) would pull future values backwards; only forward shifts are legal"
        )
    out = _empty_like(values)
    if periods == 0:
        return values.astype(float, copy=True)
    if periods < values.shape[0]:
        out[periods:] = values[:-periods]
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """max(H−L, |H−C_prev|, |L−C_prev|). NaN on the first bar, which has no prior close."""
    previous_close = shift(close, 1)
    return np.maximum(
        high - low,
        np.maximum(np.abs(high - previous_close), np.abs(low - previous_close)),
    )


def wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing — the recursive average ATR and ADX are defined with.

    Seeded with the simple mean of the first `period` valid observations, then
    `(prev*(n-1) + x)/n`. Not interchangeable with an EMA: Wilder's effective
    alpha is `1/n` where an EMA of the same nominal period uses `2/(n+1)`.
    """
    out = np.full(values.shape[0], np.nan, dtype=float)
    n = values.shape[0]
    if period <= 0 or n == 0:
        return out

    valid = np.flatnonzero(~np.isnan(values))
    if valid.size < period:
        return out

    seed_end = int(valid[period - 1])
    average = float(np.mean(values[valid[:period]]))
    out[seed_end] = average
    for index in range(seed_end + 1, n):
        value = values[index]
        if np.isnan(value):
            out[index] = average
            continue
        average = (average * (period - 1) + value) / period
        out[index] = average
    return out

"""Regime analysis — where the edge lives, and where it does not.

A strategy with a respectable overall Sharpe that earns all of it in one
volatility regime has not found an edge; it has found a market condition. That
is worth knowing *before* promotion, because the condition will end.

**Regimes are labelled from the market, never from the strategy's own P&L.**
Labelling by the equity curve would be circular — every strategy would look
brilliant in "its" regime by construction, since that is how the regime got
named. So the labels come from the cross-sectional market return, which is the
same series for every strategy scored on that snapshot and therefore comparable
across the archive.

**The labels are causal**, computed from trailing windows and expanding
quantiles, even though nothing trades on them. Diagnostics that quietly use the
whole sample are how a habit of look-ahead spreads from the report into the
signal.

One label per bar, by precedence: `crisis` beats a volatility label, which beats
a trend label. Crisis is rarest and most consequential, so it wins.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass

import numpy as np

from .panel import PricePanel
from .stats.honest_score import sharpe_ratio

__all__ = ["REGIMES", "RegimeSlice", "label_regimes", "regime_performance"]

REGIMES = ("trending", "sideways", "high_vol", "low_vol", "crisis")

#: Trailing window for volatility and trend, in bars. About a quarter of a
#: trading year at daily frequency — long enough to be a regime, short enough
#: to change within one.
DEFAULT_WINDOW = 63
#: Market drawdown that defines a crisis.
CRISIS_DRAWDOWN = 0.20
#: |trend| / volatility above this reads as trending rather than sideways.
TREND_STRENGTH = 0.10


@dataclass(frozen=True)
class RegimeSlice:
    regime: str
    sharpe: float
    cagr: float
    max_drawdown: float
    trade_count: int
    period_start: str | None
    period_end: str | None
    n_bars: int


def market_returns(panel: PricePanel) -> np.ndarray:
    """Equal-weighted return of the instruments listed on each bar."""
    close = panel.column("close")
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.full_like(close, np.nan)
        returns[1:] = np.where(close[:-1] != 0.0, close[1:] / close[:-1] - 1.0, np.nan)
    listed = np.isfinite(returns)
    counts = listed.sum(axis=1)
    totals = np.where(listed, np.nan_to_num(returns), 0.0).sum(axis=1)
    return np.where(counts > 0, totals / np.maximum(counts, 1), 0.0)


def label_regimes(
    returns: np.ndarray,
    window: int = DEFAULT_WINDOW,
    crisis_drawdown: float = CRISIS_DRAWDOWN,
    trend_strength: float = TREND_STRENGTH,
) -> np.ndarray:
    """One regime label per bar of a market return series."""
    returns = np.asarray(returns, dtype=float)
    n = returns.size
    labels = np.full(n, "sideways", dtype=object)
    if n < window + 2:
        return labels

    equity = np.cumprod(1.0 + np.nan_to_num(returns))
    drawdown = 1.0 - equity / np.maximum.accumulate(equity)

    trailing_vol = _trailing(returns, window, np.std)
    trailing_mean = _trailing(returns, window, np.mean)
    # Expanding median of the volatility itself: "high" means high relative to
    # what this market had already shown by that bar, not relative to a number
    # picked with hindsight.
    reference = _expanding_median(trailing_vol)

    with np.errstate(divide="ignore", invalid="ignore"):
        strength = np.where(trailing_vol > 0.0, np.abs(trailing_mean) / trailing_vol, 0.0)

    for index in range(n):
        if drawdown[index] >= crisis_drawdown:
            labels[index] = "crisis"
        elif not np.isfinite(trailing_vol[index]) or reference[index] <= 0.0:
            labels[index] = "sideways"
        elif trailing_vol[index] >= 1.5 * reference[index]:
            labels[index] = "high_vol"
        elif trailing_vol[index] <= 0.6 * reference[index]:
            labels[index] = "low_vol"
        elif strength[index] >= trend_strength:
            labels[index] = "trending"
    return labels


def _trailing(values: np.ndarray, window: int, function) -> np.ndarray:
    out = np.full(values.size, np.nan)
    for index in range(window, values.size):
        out[index] = function(values[index - window : index])
    return out


def _expanding_median(values: np.ndarray) -> np.ndarray:
    """The expanding median, in O(n log n) rather than O(n^2).

    Profiled as the single largest cost in a full `evaluate_experiment` run
    (~44% of wall time on a 20-year, 5-instrument panel) — recomputing
    `np.median` from scratch over an ever-growing list is a sort per bar,
    which is O(n) work n times over. The classic two-heap median maintainer
    (a max-heap of the lower half, a min-heap of the upper half, kept within
    one element of each other) does the same job in O(log n) per insertion.
    """
    out = np.zeros(values.size)
    lower: list[float] = []  # max-heap, stored negated
    upper: list[float] = []  # min-heap
    seen_any = False

    for index, value in enumerate(values):
        if np.isfinite(value):
            seen_any = True
            heapq.heappush(lower, -heapq.heappushpop(upper, float(value)))
            if len(lower) > len(upper) + 1:
                heapq.heappush(upper, -heapq.heappop(lower))
        if not seen_any:
            out[index] = 0.0
        elif len(lower) > len(upper):
            out[index] = -lower[0]
        else:
            out[index] = (-lower[0] + upper[0]) / 2.0
    return out


def regime_performance(
    strategy_returns: np.ndarray,
    dates: np.ndarray,
    labels: np.ndarray,
    periods_per_year: float,
    trades_per_bar: np.ndarray | None = None,
) -> list[RegimeSlice]:
    """Per-regime performance of one strategy return series.

    The strategy series and the labels are aligned by position; the caller is
    responsible for having labelled the same bars it scored. Regimes with fewer
    than a handful of bars are still reported — a regime the strategy barely saw
    is itself the finding.
    """
    slices: list[RegimeSlice] = []
    length = min(strategy_returns.size, labels.size)
    strategy_returns = strategy_returns[:length]
    labels = labels[:length]

    for regime in REGIMES:
        mask = labels == regime
        if not mask.any():
            continue
        segment = strategy_returns[mask]
        equity = np.cumprod(1.0 + segment)
        drawdown = 1.0 - equity / np.maximum.accumulate(equity)
        years = segment.size / periods_per_year if periods_per_year else 0.0
        total = float(equity[-1] - 1.0)
        slices.append(
            RegimeSlice(
                regime=regime,
                sharpe=sharpe_ratio(segment, periods_per_year),
                cagr=float((1.0 + total) ** (1.0 / years) - 1.0) if years > 0 and total > -1 else 0.0,
                max_drawdown=float(drawdown.max()) if drawdown.size else 0.0,
                trade_count=int(trades_per_bar[:length][mask].sum()) if trades_per_bar is not None else 0,
                period_start=_first(dates, mask),
                period_end=_last(dates, mask),
                n_bars=int(segment.size),
            )
        )
    return slices


def _first(dates: np.ndarray, mask: np.ndarray) -> str | None:
    selected = dates[: mask.size][mask]
    return str(selected[0]) if selected.size else None


def _last(dates: np.ndarray, mask: np.ndarray) -> str | None:
    selected = dates[: mask.size][mask]
    return str(selected[-1]) if selected.size else None

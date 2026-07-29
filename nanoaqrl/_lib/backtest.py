"""Stage 0's single-series backtest core.

Strategy contract (`strategy.py`):

    PARAMS: dict
    def generate_signals(df: pd.DataFrame, params: dict) -> pd.Series

`generate_signals` must return a raw signal in [-1, 1] computed using only
data available at or before each bar. **The lag itself is applied centrally,
here** — `position[t] = signal[t-1]` always — so a strategy cannot accidentally
trade on same-bar information even if it forgets to lag (TRD §2.4: "acted on
at t+1 or later"). This does not make P0 redundant: a strategy can still leak
by fitting on the whole series, `bfill()`-ing, or reading `shift(-n)` directly,
none of which the central lag can catch.

**The P0 scanners moved out at Stage 3.** They are re-exported below and now
live in `aqrl/eval/p0.py`, where the panel engine runs the same code — a second
copy of a look-ahead scanner is a scanner that stops catching things in one of
the two places, and nobody notices which.

What stays is the one-instrument, one-cost-model backtest Stage 0 was built
around. Stage 3's `aqrl/eval/backtest.py` is the panel-native engine; this
remains the reference the five-file loop runs on.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from aqrl.eval.backtest import max_drawdown_from_returns
from aqrl.eval.p0 import empirical_leakage_scan, static_lookahead_scan
from aqrl.profiles.models import CostModel

__all__ = [
    "BacktestResult",
    "SignalFn",
    "empirical_leakage_scan",
    "max_drawdown_from_returns",
    "run_backtest",
    "static_lookahead_scan",
]

SignalFn = Callable[[pd.DataFrame, dict], pd.Series]


@dataclass(frozen=True)
class BacktestResult:
    returns: np.ndarray  # net-of-cost bar returns, NaN warm-up dropped
    dates: pd.DatetimeIndex
    n_trades: int
    max_drawdown: float
    equity: pd.Series


def run_backtest(
    df: pd.DataFrame,
    generate_signals: SignalFn,
    params: dict,
    cost_model: CostModel,
    cost_multiplier: float = 2.0,
) -> BacktestResult:
    """Signal -> position -> fill -> cost -> return. TRD §7.2: costs are
    applied at 2x by default — cost stress is the default condition, not a
    separate later test."""
    signal = generate_signals(df, params).clip(-1.0, 1.0)
    position = signal.shift(1).fillna(0.0)  # the one, central, non-negotiable lag

    bar_returns = df["close"].pct_change().fillna(0.0)
    gross_returns = position * bar_returns

    position_change = position.diff().fillna(position.iloc[0])
    entry_bps = cost_model.entry_bps() * cost_multiplier
    exit_bps = cost_model.exit_bps() * cost_multiplier
    cost_bps = np.where(position_change > 0, entry_bps, exit_bps)
    cost = position_change.abs().to_numpy() * (cost_bps / 1e4)

    net_returns = gross_returns.to_numpy() - cost

    valid = ~np.isnan(net_returns)
    net_returns = net_returns[valid]
    dates = df.index[valid]

    equity = pd.Series(np.cumprod(1.0 + net_returns), index=dates)
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_dd = float(-drawdown.min()) if len(drawdown) else 0.0

    n_trades = int((position_change.to_numpy() != 0).sum())

    return BacktestResult(
        returns=net_returns,
        dates=dates,
        n_trades=n_trades,
        max_drawdown=max_dd,
        equity=equity,
    )

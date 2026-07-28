"""The backtest core + P0 checks (TRD §6, §10.1, §9.4).

Strategy contract (`strategy.py`):

    PARAMS: dict
    def generate_signals(df: pd.DataFrame, params: dict) -> pd.Series

`generate_signals` must return a raw signal in [-1, 1] computed using only
data available at or before each bar. **The lag itself is applied centrally,
here** — `position[t] = signal[t-1]` always — so a strategy cannot accidentally
trade on same-bar information even if it forgets to lag (TRD §2.4: "acted on
at t+1 or later"). This does not make P0 redundant: a strategy can still leak
by fitting on the whole series, `bfill()`-ing, or reading `shift(-n)` directly,
none of which the central lag can catch — hence the static + empirical checks
below.
"""
from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .cost_models import CostModel

SignalFn = Callable[[pd.DataFrame, dict], pd.Series]


@dataclass(frozen=True)
class BacktestResult:
    returns: np.ndarray  # net-of-cost bar returns, NaN warm-up dropped
    dates: pd.DatetimeIndex
    n_trades: int
    max_drawdown: float
    equity: pd.Series


# ---------------------------------------------------------------------------
# P0a — static look-ahead scan (AST-based, TRD §9.4 / §10.1)
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_NAMES = {"bfill", "backfill"}
_UNROLLED_STAT_METHODS = {"mean", "std", "var", "zscore", "rank"}


class _LookaheadVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id

        if name in _FORBIDDEN_CALL_NAMES:
            self.violations.append(f"line {node.lineno}: forbidden call `{name}()` (pulls the future backwards)")

        if name == "shift" and node.args:
            arg = node.args[0]
            # Kept as two explicit branches. Collapsing them into one
            # `A and B or C and D` chain relies on operator precedence, and this
            # is the look-ahead scanner — the one place in the codebase where a
            # subtly misread condition silently stops catching leaks.
            if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                self.violations.append(f"line {node.lineno}: `shift(-n)` is forbidden anywhere")
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)) and arg.value < 0:
                self.violations.append(f"line {node.lineno}: `shift(-n)` is forbidden anywhere")

        if name == "fillna":
            for kw in node.keywords:
                if kw.arg == "method" and isinstance(kw.value, ast.Constant) and kw.value.value in ("backfill", "bfill"):
                    self.violations.append(f"line {node.lineno}: `fillna(method='{kw.value.value}')` is forbidden")

        if name in ("rolling", "expanding", "ewm"):
            for kw in node.keywords:
                if kw.arg == "center" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    self.violations.append(f"line {node.lineno}: centred windows (`center=True`) are forbidden")

        # Whole-series stats: `.mean()/.std()/...` NOT chained off `.rolling(/.expanding(/.ewm(`.
        if name in _UNROLLED_STAT_METHODS and isinstance(func, ast.Attribute):
            receiver = func.value
            chained_on_window = isinstance(receiver, ast.Call) and (
                isinstance(receiver.func, ast.Attribute) and receiver.func.attr in ("rolling", "expanding", "ewm")
            )
            if not chained_on_window:
                self.violations.append(
                    f"line {node.lineno}: whole-series `.{name}()` — rolling statistics only, never over the full series"
                )

        self.generic_visit(node)


def static_lookahead_scan(source: str) -> list[str]:
    """P0a. Returns a list of violation strings; empty means the scan passed."""
    tree = ast.parse(source)
    visitor = _LookaheadVisitor()
    visitor.visit(tree)
    return visitor.violations


# ---------------------------------------------------------------------------
# P0b — empirical leakage scan: truncation invariance
# ---------------------------------------------------------------------------


def empirical_leakage_scan(
    df: pd.DataFrame,
    generate_signals: SignalFn,
    params: dict,
    checkpoints: tuple[float, ...] = (0.5, 0.7, 0.85),
    tolerance: float = 1e-9,
) -> list[str]:
    """P0b. A signal computed with only data up to bar t must be identical
    whether or not the strategy was ever shown bars after t. Truncate the
    frame at several checkpoints and compare against the full-sample signal
    over the overlapping region — any mismatch means the strategy peeked at
    the future somewhere the static scan didn't catch."""
    violations: list[str] = []
    full_signal = generate_signals(df, params)
    n = len(df)

    for frac in checkpoints:
        k = int(n * frac)
        if k < 10:
            continue
        truncated = generate_signals(df.iloc[:k], params)
        overlap = min(len(truncated), k)
        a = full_signal.iloc[:overlap].to_numpy(dtype=float)
        b = truncated.iloc[:overlap].to_numpy(dtype=float)
        # Ignore the tail of the truncated window — a rolling window's most
        # recent points can legitimately differ once enough of it is missing
        # relative to the warm-up; compare only the stable prefix.
        stable = max(overlap - 5, 0)
        if stable == 0:
            continue
        a, b = a[:stable], b[:stable]
        mask = ~(np.isnan(a) & np.isnan(b))
        if not np.allclose(np.nan_to_num(a[mask]), np.nan_to_num(b[mask]), atol=tolerance):
            violations.append(
                f"truncation at {frac:.0%} of history changed earlier signal values — future data leaked backwards"
            )
    return violations


# ---------------------------------------------------------------------------
# Backtest core
# ---------------------------------------------------------------------------


def max_drawdown_from_returns(returns: np.ndarray) -> float:
    if returns.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + returns)
    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    return float(-drawdown.min())


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

"""The backtest core — signal → position → fill → cost → return.

Three properties this module is responsible for, each of which is a stated
requirement somewhere in the docs and a silent disaster if it drifts:

**1. The lag is central and non-negotiable.** `position[t] = signal[t-1]`,
applied here and nowhere else. A spec cannot forget it, and a spec cannot
disable it. This is not a substitute for P0 — a strategy can still leak by
fitting on the whole series or reading a future bar directly, neither of which a
lag can catch — but it removes the single most common accident (TRD §9.4).

**2. Gross exposure is capped at 1× equity.** Weights are scaled down only when
`Σ|position| > 1`, never up. Scaling up would override the sizing a `vol_target`
or `kelly` operator deliberately chose; leaving the cap off would let a strategy
manufacture Sharpe with leverage, which §7.4 names as exactly the reason the
score is a ratio rather than a return.

**3. Positions exist only where prices do.** An instrument with no bar is not
tradeable on that bar, so its weight is zero. Nothing is forward-filled: a
forward fill invents prices on days a stock was not listed and the backtest
trades them at those invented prices.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..profiles.models import ResolvedProfile
from .costs import total_costs, traded_split
from .fills import entry_returns, exit_returns, fill_prices, holding_returns
from .panel import PricePanel

__all__ = ["BacktestResult", "run_backtest"]


@dataclass(frozen=True)
class BacktestResult:
    """One backtest over one panel, at one cost multiplier."""

    dates: np.ndarray
    instruments: tuple[str, ...]
    weights: np.ndarray  # (n_bars, n_instruments), post-lag, post-cap
    gross_returns: np.ndarray  # (n_bars, n_instruments)
    costs: np.ndarray  # (n_bars, n_instruments)
    net_returns: np.ndarray  # (n_bars, n_instruments)
    portfolio_returns: np.ndarray  # (n_bars,)
    equity: np.ndarray  # (n_bars,)
    cost_multiplier: float
    #: Bars where a held position met a non-finite return. Zero is the only
    #: acceptable value; P0 gates on it rather than letting NaN reach the score.
    non_finite_bars: int

    @property
    def n_bars(self) -> int:
        return int(self.portfolio_returns.size)

    @property
    def n_trades(self) -> int:
        """Round trips initiated: transitions from flat-or-opposite into a side."""
        side = np.sign(self.weights)
        previous = np.zeros_like(side)
        previous[1:] = side[:-1]
        return int(((side != 0.0) & (side != previous)).sum())

    @property
    def instrument_trades(self) -> np.ndarray:
        side = np.sign(self.weights)
        previous = np.zeros_like(side)
        previous[1:] = side[:-1]
        return ((side != 0.0) & (side != previous)).sum(axis=0)

    @property
    def instrument_pnl(self) -> np.ndarray:
        """Net contribution per instrument — the input to the breadth gate."""
        return self.net_returns.sum(axis=0)

    @property
    def max_drawdown(self) -> float:
        return max_drawdown_from_returns(self.portfolio_returns)

    @property
    def turnover(self) -> float:
        """Annualisation-free: total absolute weight traded, in units of equity."""
        previous = np.zeros_like(self.weights)
        previous[1:] = self.weights[:-1]
        return float(np.abs(self.weights - previous).sum())

    @property
    def exposure(self) -> float:
        """Mean gross exposure — the fraction of equity at risk on average."""
        if not self.n_bars:
            return 0.0
        return float(np.abs(self.weights).sum(axis=1).mean())


def max_drawdown_from_returns(returns: np.ndarray) -> float:
    """Peak-to-trough, as a positive fraction. Empty series have no drawdown."""
    returns = np.asarray(returns, dtype=float)
    if returns.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + returns)
    running_max = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = (equity - running_max) / running_max
    return float(-np.nanmin(drawdown)) if np.isfinite(drawdown).any() else 0.0


def lag_and_cap(signals: np.ndarray, tradeable: np.ndarray) -> np.ndarray:
    """Apply the central lag, zero untradeable bars, cap gross exposure at 1."""
    positions = np.zeros_like(signals)
    positions[1:] = signals[:-1]
    positions = np.nan_to_num(positions, nan=0.0, posinf=0.0, neginf=0.0)
    positions = np.where(tradeable, positions, 0.0)
    positions = np.clip(positions, -1.0, 1.0)

    gross = np.abs(positions).sum(axis=1, keepdims=True)
    scale = np.where(gross > 1.0, 1.0 / np.maximum(gross, 1e-12), 1.0)
    return positions * scale


def run_backtest(
    panel: PricePanel,
    signals: np.ndarray,
    resolved: ResolvedProfile,
    cost_multiplier: float = 2.0,
) -> BacktestResult:
    """Score one panel of signals under one resolved profile.

    `signals` is `(n_bars, n_instruments)` in `[-1, 1]`, computed from data at
    or before each bar. Everything after that is this function's business.
    """
    if signals.ndim == 1:
        signals = signals.reshape(-1, 1)
    if signals.shape != (panel.n_bars, panel.n_instruments):
        raise ValueError(
            f"signals shape {signals.shape} does not match panel "
            f"({panel.n_bars}, {panel.n_instruments})"
        )

    weights = lag_and_cap(signals, panel.listed())

    fills = fill_prices(panel, resolved.timeframe.fill_model)
    held, opened, closed = traded_split(weights)
    ret_hold = holding_returns(panel)
    ret_entry = entry_returns(panel, fills)
    ret_exit = exit_returns(panel, fills)

    gross = (
        _weighted(held, ret_hold) + _weighted(opened, ret_entry) + _weighted(closed, ret_exit)
    )
    non_finite = int(
        (((held != 0.0) & ~np.isfinite(ret_hold)) | ((opened != 0.0) & ~np.isfinite(ret_entry))).sum()
    )

    costs = total_costs(
        weights, resolved.cost_model, resolved.timeframe.bar_seconds, cost_multiplier
    )
    net = gross - costs
    portfolio = net.sum(axis=1)

    # Bar 0 has no prior close, so no return is defined for it. Dropping it
    # keeps the equity curve and the scored series the same length as their
    # dates, which is what stops an off-by-one appearing in a fold join.
    keep = slice(1, None)
    portfolio = portfolio[keep]
    equity = np.cumprod(1.0 + portfolio)

    return BacktestResult(
        dates=panel.dates[keep],
        instruments=panel.instruments,
        weights=weights[keep],
        gross_returns=gross[keep],
        costs=costs[keep],
        net_returns=net[keep],
        portfolio_returns=portfolio,
        equity=equity,
        cost_multiplier=cost_multiplier,
        non_finite_bars=non_finite,
    )


def _weighted(quantity: np.ndarray, returns: np.ndarray) -> np.ndarray:
    """`quantity × returns`, with a zero quantity beating a non-finite return.

    `0 × NaN` is NaN, and one NaN in a portfolio sum destroys the whole series.
    A zero weight means "not holding this", which is a fact about the position
    and not a claim about the price, so it wins.
    """
    product = np.where(quantity != 0.0, quantity * np.nan_to_num(returns, nan=0.0), 0.0)
    return np.nan_to_num(product, nan=0.0, posinf=0.0, neginf=0.0)

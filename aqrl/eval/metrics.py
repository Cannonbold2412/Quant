"""Core metrics — the report, not the decision.

Thirty metrics is a report; it cannot answer *"is experiment 47 better than
46?"* (TRD §7.1). Everything here is computed, stored and queryable, and none of
it drives the loop — that is the honest score's job alone. They earn their place
by explaining *why* a result looks the way it does, which is what A3 reasons over
and what A5 mines across the archive.

**Annualisation comes from the profile, always.** `periods_per_year` is derived
from the market calendar and the bar size at resolution time; a hardcoded 252 in
this file would be a project-level bug, because the same code annualises 1-second
NSE bars (5,670,000/yr) and monthly crypto bars (12/yr). `tests/eval/` asserts
the constant does not appear here.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from .backtest import BacktestResult, max_drawdown_from_returns
from .stats.honest_score import sharpe_ratio
from .tradebook import Trade, closed_trades

__all__ = ["CoreMetrics", "compute_metrics", "drawdown_series"]


@dataclass(frozen=True)
class CoreMetrics:
    """One row's worth of `evaluations` columns."""

    sharpe: float
    sortino: float
    calmar: float
    cagr: float
    total_return: float
    max_drawdown: float
    avg_drawdown: float
    dd_duration_days: float
    profit_factor: float
    win_rate: float
    expectancy: float
    trade_count: int
    avg_trade_return: float
    turnover: float
    exposure_pct: float

    def row(self) -> dict:
        return asdict(self)


def drawdown_series(returns: np.ndarray) -> np.ndarray:
    """Drawdown at every bar, as a non-positive fraction of the running peak."""
    if returns.size == 0:
        return np.array([])
    equity = np.cumprod(1.0 + returns)
    running_max = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (equity - running_max) / running_max


def longest_drawdown_days(returns: np.ndarray, dates: np.ndarray) -> float:
    """Calendar days of the longest peak-to-recovery stretch.

    Measured in **calendar** days rather than bars because that is what the
    column is called and what a human reads it as. An unrecovered drawdown
    running to the end of the sample counts to the last bar — it is the longest
    one observed, and pretending otherwise would flatter a strategy still
    underwater when the data ran out.
    """
    if returns.size == 0:
        return 0.0
    underwater = drawdown_series(returns) < 0.0
    current_start: int | None = None
    longest = 0.0
    for index, is_under in enumerate(underwater):
        if is_under and current_start is None:
            current_start = index
        elif not is_under and current_start is not None:
            longest = max(longest, _days_between(dates, current_start, index))
            current_start = None
    if current_start is not None:
        longest = max(longest, _days_between(dates, current_start, len(underwater) - 1))
    return float(longest)


def _days_between(dates: np.ndarray, start: int, end: int) -> float:
    if dates is None or len(dates) <= end:
        return float(end - start)
    delta = np.datetime64(dates[end], "D") - np.datetime64(dates[start], "D")
    return float(delta / np.timedelta64(1, "D"))


def compute_metrics(
    result: BacktestResult,
    trades: list[Trade],
    periods_per_year: float,
) -> CoreMetrics:
    """Every P1/P2 metric, from one backtest and its tradebook."""
    returns = result.portfolio_returns
    n = returns.size

    sharpe = sharpe_ratio(returns, periods_per_year)
    downside = returns[returns < 0.0]
    downside_deviation = float(downside.std(ddof=1)) if downside.size > 1 else 0.0
    sortino = (
        float(returns.mean() / downside_deviation * math.sqrt(periods_per_year))
        if downside_deviation > 0.0
        else 0.0
    )

    total_return = float(np.prod(1.0 + returns) - 1.0) if n else 0.0
    # Elapsed time from the bar count and the profile's own annualisation, not
    # from the calendar span: a concatenated walk-forward series has embargo
    # gaps cut out of it, and calendar span would count time the strategy was
    # not invested in.
    years = n / periods_per_year if periods_per_year else 0.0
    cagr = float((1.0 + total_return) ** (1.0 / years) - 1.0) if years > 0 and total_return > -1 else 0.0

    max_dd = max_drawdown_from_returns(returns)
    drawdowns = drawdown_series(returns)
    underwater = drawdowns[drawdowns < 0.0]
    avg_dd = float(-underwater.mean()) if underwater.size else 0.0
    calmar = float(cagr / max_dd) if max_dd > 0.0 else 0.0

    settled = closed_trades(trades)
    wins = [trade.net_return for trade in settled if trade.net_return > 0.0]
    losses = [trade.net_return for trade in settled if trade.net_return < 0.0]
    gross_loss = abs(sum(losses))
    profit_factor = float(sum(wins) / gross_loss) if gross_loss > 0.0 else 0.0
    win_rate = float(len(wins) / len(settled)) if settled else 0.0
    expectancy = float(np.mean([trade.net_return for trade in settled])) if settled else 0.0

    return CoreMetrics(
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        cagr=cagr,
        total_return=total_return,
        max_drawdown=max_dd,
        avg_drawdown=avg_dd,
        dd_duration_days=longest_drawdown_days(returns, result.dates),
        profit_factor=profit_factor,
        win_rate=win_rate,
        expectancy=expectancy,
        trade_count=len(trades),
        avg_trade_return=expectancy,
        turnover=result.turnover,
        exposure_pct=result.exposure,
    )

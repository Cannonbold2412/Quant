"""Round trips and the Parquet artifacts.

The scored series is a stream of bar returns; a *trade* is a different object,
and several things downstream need it — win rate, profit factor and expectancy
are per-trade statistics, and the minimum-trade-count bar item counts round
trips rather than bars.

A round trip opens when an instrument's side moves from flat or from the
opposite side, and closes when it returns to flat or flips again. Scaling in and
out within a side is **one** trade, not several: a strategy that halves its
position and restores it has not taken a second view, and counting it as two
would inflate the trade count against a bar that exists precisely to stop that.

A trade still open at the end of the sample is recorded with `open_at_end` set.
It counts for turnover and P&L but is excluded from the per-trade statistics —
its outcome is not known yet, and letting an unrealised winner into the win rate
is a small, cheerful lie.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import polars as pl

from .backtest import BacktestResult

__all__ = ["Trade", "extract_trades", "write_equity_curve", "write_tradebook"]


@dataclass(frozen=True)
class Trade:
    instrument: str
    side: int  # +1 long, -1 short
    entry_date: str
    exit_date: str
    bars_held: int
    avg_weight: float
    gross_return: float
    cost: float
    net_return: float
    open_at_end: bool


def extract_trades(result: BacktestResult) -> list[Trade]:
    """Every round trip in a backtest, in chronological order."""
    trades: list[Trade] = []
    sides = np.sign(result.weights)

    for column, instrument in enumerate(result.instruments):
        side_series = sides[:, column]
        # Boundaries are where the side changes; everything between two
        # boundaries is one trade.
        changed = np.empty(side_series.size, dtype=bool)
        changed[0] = side_series[0] != 0.0
        changed[1:] = side_series[1:] != side_series[:-1]
        starts = np.flatnonzero(changed & (side_series != 0.0))

        for start in starts:
            end = start + 1
            while end < side_series.size and side_series[end] == side_series[start]:
                end += 1
            open_at_end = end == side_series.size
            trades.append(
                Trade(
                    instrument=instrument,
                    side=int(side_series[start]),
                    entry_date=str(result.dates[start]),
                    exit_date=str(result.dates[min(end, side_series.size - 1)]),
                    bars_held=int(end - start),
                    avg_weight=float(np.abs(result.weights[start:end, column]).mean()),
                    gross_return=float(result.gross_returns[start:end, column].sum()),
                    cost=float(result.costs[start:end, column].sum()),
                    net_return=float(result.net_returns[start:end, column].sum()),
                    open_at_end=bool(open_at_end),
                )
            )

    trades.sort(key=lambda trade: (trade.entry_date, trade.instrument))
    return trades


def closed_trades(trades: list[Trade]) -> list[Trade]:
    return [trade for trade in trades if not trade.open_at_end]


def trades_frame(trades: list[Trade]) -> pl.DataFrame:
    if not trades:
        return pl.DataFrame(
            schema={
                "instrument": pl.Utf8,
                "side": pl.Int64,
                "entry_date": pl.Utf8,
                "exit_date": pl.Utf8,
                "bars_held": pl.Int64,
                "avg_weight": pl.Float64,
                "gross_return": pl.Float64,
                "cost": pl.Float64,
                "net_return": pl.Float64,
                "open_at_end": pl.Boolean,
            }
        )
    return pl.DataFrame([asdict(trade) for trade in trades])


def write_tradebook(path: Path | str, trades: list[Trade]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    trades_frame(trades).write_parquet(path)
    return path


def write_equity_curve(path: Path | str, result: BacktestResult) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "date": result.dates.astype("datetime64[D]"),
            "return": result.portfolio_returns,
            "equity": result.equity,
            "gross_exposure": np.abs(result.weights).sum(axis=1),
            "cost": result.costs.sum(axis=1),
        }
    ).write_parquet(path)
    return path

"""The fill model — at what price a change in position is transacted.

`TimeframeProfile.fill_model` decides one array: the **fill price** at which the
transition into `position[t]` happens. Everything else in the backtest is
identical across fill models, which is the point — a fill model is a profile
field, not a fork of the engine (TRD §6.2).

The convention, stated once because every look-ahead argument in this codebase
turns on it:

* `signal[t]` is computed from data **at or before** bar `t`'s close.
* `position[t] = signal[t-1]` — the one central lag, applied in `backtest.py`.
* `ret[t] = close[t] / close[t-1] − 1` is the return realised **over** bar `t`.
* `fill_price[t]` is what the transition into `position[t]` costs.

| Fill model | `fill_price[t]` | Reading |
|---|---|---|
| `bar_close` | `close[t-1]` | Ordered and filled on the signal bar's close; the new position earns all of bar `t`. |
| `next_open` / `next_bar_open` | `open[t]` | Ordered on the signal bar's close, filled at the next open; the new position earns only `open[t] → close[t]`. |
| `vwap` | `(high[t] + low[t] + close[t]) / 3` | A typical-price proxy — see the caveat below. |
| `queue_position` | — | Refused: bar data cannot represent it. |

Under `bar_close` the arithmetic collapses back to the naive
`position × close-to-close return`, so the more realistic models are strictly
more conservative rather than a different accounting.

**The VWAP caveat.** A true VWAP fill needs intrabar volume distribution, which
an OHLCV bar does not carry. `(H+L+C)/3` is the standard proxy and it is a
*proxy* — named here rather than buried, because a fill model that quietly
flatters itself is the same class of error as a cost model sourced from a blog.
"""
from __future__ import annotations

import numpy as np

from ..profiles.models import FillModel
from .panel import PricePanel

__all__ = ["FillError", "fill_prices", "holding_returns"]


class FillError(ValueError):
    """A fill model that cannot be honestly represented by the available data."""


def fill_prices(panel: PricePanel, model: FillModel) -> np.ndarray:
    """`(n_bars, n_instruments)` transaction price for entering `position[t]`."""
    close = panel.column("close")

    if model == "bar_close":
        prices = np.full_like(close, np.nan)
        prices[1:] = close[:-1]
        return prices

    if model in ("next_open", "next_bar_open"):
        return panel.column("open").copy()

    if model == "vwap":
        return (panel.column("high") + panel.column("low") + close) / 3.0

    if model == "queue_position":
        raise FillError(
            "fill_model 'queue_position' cannot be simulated from bar data: fills depend on "
            "queue position and latency that OHLCV does not carry (TRD §13.2). Supported is "
            "not the same as trustworthy — use tick data or a coarser fill model."
        )

    raise FillError(f"unknown fill model {model!r}")


def holding_returns(panel: PricePanel) -> np.ndarray:
    """`close[t] / close[t-1] − 1` — the return of a position held all bar."""
    close = panel.column("close")
    returns = np.full_like(close, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        returns[1:] = np.where(close[:-1] != 0.0, close[1:] / close[:-1] - 1.0, np.nan)
    return returns


def entry_returns(panel: PricePanel, fills: np.ndarray) -> np.ndarray:
    """`close[t] / fill_price[t] − 1` — earned by a position opened during bar `t`."""
    close = panel.column("close")
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(fills != 0.0, close / fills - 1.0, np.nan)


def exit_returns(panel: PricePanel, fills: np.ndarray) -> np.ndarray:
    """`fill_price[t] / close[t-1] − 1` — earned by a position closed during bar `t`."""
    close = panel.column("close")
    previous = np.full_like(close, np.nan)
    previous[1:] = close[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(previous != 0.0, fills / previous - 1.0, np.nan)

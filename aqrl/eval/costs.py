"""Cost application — the resolved `(market, asset_class)` cost model, applied.

Two things are charged, and they are charged differently:

* **Transaction costs**, on the quantity that changes hands. The entry and exit
  sides are *not* symmetric — on NSE cash delivery STT is levied on both sides
  but stamp duty only on the buy, and a flat DP charge lands only on the sell —
  so the two sides are computed separately from `CostModel.entry_bps()` and
  `CostModel.exit_bps()` rather than halving a round-trip number.
* **Funding**, on the exposure held, for perpetuals. Time-based, not
  trade-based: a position that never trades still pays it.

**Sign changes are two transactions, not one.** Going from +1 to −1 closes a
long and opens a short: it pays the exit side on the first and the entry side on
the second. Charging `|Δw| = 2` at one rate would misprice every reversal in a
long/short strategy, and reversals are exactly what a crossover strategy does
all day.

**2× is the default multiplier, not a later stress test** (TRD §7.2). The
honest score is computed under cost stress by construction; running at 1× is the
diagnostic, not the headline.
"""
from __future__ import annotations

import numpy as np

from ..profiles.models import CostModel

__all__ = [
    "funding_costs",
    "total_costs",
    "traded_quantities",
    "traded_split",
    "transaction_costs",
]

BPS = 1e-4
SECONDS_PER_DAY = 86_400.0


def traded_split(weights: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Decompose each bar's weight into `(held, opened, closed)`, **signed**.

    * `held` — carried in from the previous bar and still on, so it earns the
      whole bar.
    * `opened` — established during this bar, so it earns from the fill price.
    * `closed` — given up during this bar, so it earns only up to the fill price.

    Signed rather than absolute because `backtest.py` multiplies these straight
    into returns, and a short's sign has to survive the decomposition. A sign
    flip populates `opened` and `closed` simultaneously — that is the case a
    naive `Δw` collapses into one number and misprices.
    """
    previous = np.zeros_like(weights)
    previous[1:] = weights[:-1]
    same_side = previous * weights > 0.0

    magnitude, previous_magnitude = np.abs(weights), np.abs(previous)
    held = np.where(same_side, np.sign(weights) * np.minimum(previous_magnitude, magnitude), 0.0)
    opened = np.where(
        same_side, np.sign(weights) * np.maximum(magnitude - previous_magnitude, 0.0), weights
    )
    closed = np.where(
        same_side, np.sign(previous) * np.maximum(previous_magnitude - magnitude, 0.0), previous
    )
    return held, opened, closed


def traded_quantities(weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The `(opened, closed)` magnitudes that transaction costs are charged on."""
    _, opened, closed = traded_split(weights)
    return np.abs(opened), np.abs(closed)


def transaction_costs(
    weights: np.ndarray,
    cost_model: CostModel,
    multiplier: float = 2.0,
) -> np.ndarray:
    """`(n_bars, n_instruments)` cost as a fraction of portfolio equity."""
    opened, closed = traded_quantities(weights)
    entry = cost_model.entry_bps() * BPS * multiplier
    exit_ = cost_model.exit_bps() * BPS * multiplier
    return opened * entry + closed * exit_


def funding_costs(
    weights: np.ndarray,
    cost_model: CostModel,
    bar_seconds: float,
    multiplier: float = 2.0,
) -> np.ndarray:
    """Funding accrued on open exposure over one bar.

    Charged on `|w|` regardless of side. A perpetual's funding rate flips sign
    with the basis, so which side pays is a *market-state* question this profile
    field does not model; charging the absolute exposure is the conservative
    reading and never flatters a backtest.
    """
    if not cost_model.funding_rate_bps_per_day:
        return np.zeros_like(weights)
    per_bar = cost_model.funding_rate_bps_per_day * BPS * (bar_seconds / SECONDS_PER_DAY)
    return np.abs(weights) * per_bar * multiplier


def total_costs(
    weights: np.ndarray,
    cost_model: CostModel,
    bar_seconds: float,
    multiplier: float = 2.0,
) -> np.ndarray:
    return transaction_costs(weights, cost_model, multiplier) + funding_costs(
        weights, cost_model, bar_seconds, multiplier
    )

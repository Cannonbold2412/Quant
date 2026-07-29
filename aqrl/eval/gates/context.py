"""`GateContext` — everything a market gate might need, gathered once.

A shared bundle rather than a long parameter list on every gate function,
because the gate set that applies to one experiment varies (§6.5), and a
common signature is what lets `run_market_gates` call them uniformly.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..backtest import BacktestResult
from ..panel import PricePanel
from ...profiles.models import ResolvedProfile

__all__ = ["GateContext"]


@dataclass(frozen=True)
class GateContext:
    """One evaluated experiment's inputs, as every market gate sees them."""

    panel: PricePanel
    result: BacktestResult
    resolved: ResolvedProfile
    #: Whether this snapshot's data has had its known survivorship bias
    #: mitigated (point-in-time membership + full ever-member history).
    survivorship_handled: bool = False
    #: A second venue's return series for the same strategy, when available.
    #: `None` is the honest, common case today — no market ships multi-venue
    #: data yet — and the crypto gate reports that rather than fabricating a
    #: verdict.
    secondary_venue_returns: np.ndarray | None = None
    #: Assumed capital for the capacity/ADV proxy, in the market's currency.
    #: Provisional (`Implementation_Plan.md` §21 owns the real definition of
    #: breadth and complexity; capacity has the same "measured, not invented"
    #: treatment here).
    assumed_capital: float = 10_000_000.0
    #: True for a market whose calendar is forex-shaped (24/5, no daily
    #: session boundary). Explicit rather than inferred from the name, since
    #: no forex `MarketProfile` ships yet.
    is_forex: bool = False
    #: The same strategy's return series under a different, legitimate futures
    #: roll method. `None` is the honest default — no market ships more than
    #: one roll-adjusted series today.
    alternate_roll_returns: np.ndarray | None = None

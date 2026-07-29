"""The `SliceEvaluator` the panel engine hands to `walk_forward.py`.

`walk_forward.py` knows nothing about panels, specs or profiles — it only
knows `(start, end, params) -> SliceOutcome`. This module is the adapter that
makes a compiled spec answer that call: slice the panel to the date range,
evaluate the spec per instrument, run the panel backtest, and report gross
returns and costs alongside the net series so `robustness.py`'s cost sweep
never needs a second backtest.

`params` overrides node parameters by `"<node_id>.<param>"` key
(`CompiledSpec`'s own convention — see `aqrl/operators/compile.py`), which is
exactly the shape the walk-forward's tuning grid produces.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..operators.compile import CompiledSpec
from ..profiles.models import ResolvedProfile
from .backtest import run_backtest
from .panel import PricePanel
from .walk_forward import SliceOutcome

__all__ = ["PanelEvaluator"]


@dataclass(frozen=True)
class PanelEvaluator:
    """A picklable `SliceEvaluator` over one panel and one compiled spec.

    Picklable because `parallel.py`'s process pool needs to ship work to
    workers — a closure over the panel would not survive that.
    """

    panel: PricePanel
    compiled: CompiledSpec
    resolved: ResolvedProfile
    cost_multiplier: float = 2.0

    def __call__(self, start, end, params: dict) -> SliceOutcome:
        window = self.panel.slice_dates(start, end)
        if window.n_bars == 0:
            empty = np.array([])
            return SliceOutcome(empty, np.array([], dtype="datetime64[D]"), 0)

        signals = np.column_stack(
            [
                self.compiled.signals_array(window.instrument_columns(index), params)
                for index in range(window.n_instruments)
            ]
        )
        result = run_backtest(window, signals, self.resolved, self.cost_multiplier)
        gross = result.gross_returns.sum(axis=1)
        costs = result.costs.sum(axis=1)
        return SliceOutcome(
            returns=result.portfolio_returns,
            dates=result.dates,
            n_trades=result.n_trades,
            gross_returns=gross,
            costs=costs / max(self.cost_multiplier, 1e-9),  # normalise back to 1x
        )

"""Stage 0's walk-forward entry point. **The protocol lives elsewhere.**

Stage 3 moved the protocol — fold geometry, purge and embargo, tuning on the
training window only, chronological concatenation, best-of-three — into
`aqrl/eval/walk_forward.py`, where it is shared with the panel engine. TRD §6.1
requires exactly that: two implementations of the same statistical procedure
diverge silently, and then a Sharpe of 1.4 stops meaning the same thing in two
rows of the same table.

What stays here is the **adapter**: nanoAQRL evaluates one pandas series with a
`generate_signals(df, params)` function, so this module turns a date range into
a return series in those terms and hands the protocol that callable. The
protocol never learns which shape of data it is scoring, which is the point.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from aqrl.eval.walk_forward import (
    MAX_GRID_COMBINATIONS,
    BestOfThreeResult,
    FoldResult,
    FoldWindow,
    SliceOutcome,
    WindowResult,
    generate_rolling_folds,
)
from aqrl.eval.walk_forward import run_best_of_three as _run_best_of_three
from aqrl.eval.walk_forward import run_window as _run_window
from aqrl.profiles.models import CostModel

from .backtest import SignalFn, run_backtest

__all__ = [
    "MAX_GRID_COMBINATIONS",
    "BestOfThreeResult",
    "FoldResult",
    "FoldWindow",
    "WindowResult",
    "generate_rolling_folds",
    "run_best_of_three",
    "run_window",
]

#: Stage 0's floor: never shorter than a working week, whatever the estimate
#: says. A one-day embargo on a strategy that holds for three would leak.
MIN_EMBARGO_DAYS = 5


@dataclass(frozen=True)
class _PandasEvaluator:
    """Turn `(start, end, params)` into a return series over one pandas frame."""

    df: pd.DataFrame
    generate_signals: SignalFn
    cost_model: CostModel
    cost_multiplier: float

    def __call__(self, start: date, end: date, params: dict) -> SliceOutcome:
        window = self.df.loc[str(start) : str(end)]
        if window.empty:
            return SliceOutcome(np.array([]), np.array([], dtype="datetime64[D]"), 0)
        result = run_backtest(
            window, self.generate_signals, params, self.cost_model, self.cost_multiplier
        )
        return SliceOutcome(
            returns=result.returns,
            dates=result.dates.to_numpy().astype("datetime64[D]"),
            n_trades=result.n_trades,
        )


def _with_pandas_dates(window: WindowResult) -> WindowResult:
    """Re-wrap the concatenated dates as a `DatetimeIndex`.

    The engine works in `datetime64` arrays; Stage 0 and its tests expect a
    pandas index. Converting at this boundary keeps both honest.
    """
    return dataclasses.replace(
        window, concatenated_dates=pd.DatetimeIndex(window.concatenated_dates)
    )


def run_window(
    df: pd.DataFrame,
    generate_signals: SignalFn,
    base_params: dict,
    cost_model: CostModel,
    train_years: int,
    test_years: int,
    embargo_days: int,
    n_trials: int,
    param_grid: dict[str, list] | None = None,
    cost_multiplier: float = 2.0,
    periods_per_year: float = 252.0,
    z_multiplier: float = 1.65,
) -> WindowResult:
    evaluator = _PandasEvaluator(df, generate_signals, cost_model, cost_multiplier)
    return _with_pandas_dates(
        _run_window(
            dates=df.index,
            evaluate=evaluator,
            base_params=base_params,
            train_years=train_years,
            test_years=test_years,
            embargo_days=embargo_days,
            n_trials=n_trials,
            param_grid=param_grid,
            periods_per_year=periods_per_year,
            z_multiplier=z_multiplier,
        )
    )


def run_best_of_three(
    df: pd.DataFrame,
    generate_signals: SignalFn,
    base_params: dict,
    cost_model: CostModel,
    holding_period_days: int,
    n_trials_base: int,
    param_grid: dict[str, list] | None = None,
    test_years: int = 1,
    cost_multiplier: float = 2.0,
    periods_per_year: float = 252.0,
    z_multiplier: float = 1.65,
) -> BestOfThreeResult:
    """TRD §8.2: run all three train windows, `N_trials` tripled because taking
    the best of three is itself a selection on the reported metric."""
    evaluator = _PandasEvaluator(df, generate_signals, cost_model, cost_multiplier)
    result = _run_best_of_three(
        dates=df.index,
        evaluate=evaluator,
        base_params=base_params,
        holding_period_bars=holding_period_days,
        n_trials=n_trials_base * 3,
        param_grid=param_grid,
        test_years=test_years,
        periods_per_year=periods_per_year,
        z_multiplier=z_multiplier,
        embargo_days=max(holding_period_days, MIN_EMBARGO_DAYS),
    )
    return dataclasses.replace(
        result,
        windows={years: _with_pandas_dates(window) for years, window in result.windows.items()},
    )

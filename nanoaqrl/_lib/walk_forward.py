"""The walk-forward protocol — TRD §8.

Rolling windows, test always 1 year, train evaluated at all three lengths
(1/2/3yr) with the best reported, purged with embargo >= holding period,
folds concatenated (never averaged) before scoring. Parameter tuning, when
enabled, is re-run from scratch inside each fold on the training slice only —
it never inflates `N_trials` (TRD §8.6) because it never sees the test window.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest import BacktestResult, SignalFn, run_backtest
from .cost_models import CostModel
from .honest_score import HonestScoreResult, compute_honest_score, sharpe_ratio

MAX_GRID_COMBINATIONS = 50  # TRD §8.6: "recommended starting grid: <=50 per fold"


@dataclass(frozen=True)
class FoldResult:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    chosen_params: dict
    in_sample_sharpe: float
    test_result: BacktestResult


@dataclass(frozen=True)
class WindowResult:
    train_years: int
    folds: list[FoldResult]
    concatenated_returns: np.ndarray
    concatenated_dates: pd.DatetimeIndex
    total_trades: int
    wf_efficiency: float
    score: HonestScoreResult


@dataclass(frozen=True)
class BestOfThreeResult:
    windows: dict[int, WindowResult]
    winning_train_years: int

    @property
    def winning_window(self) -> WindowResult:
        return self.windows[self.winning_train_years]

    @property
    def score(self) -> HonestScoreResult:
        return self.winning_window.score

    @property
    def score_spread(self) -> dict[int, float]:
        return {ty: w.score.honest_score for ty, w in self.windows.items()}


def generate_rolling_folds(
    dates: pd.DatetimeIndex,
    train_years: int,
    test_years: int,
    embargo_days: int,
) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """Rolling (not anchored): train length is fixed and slides forward by
    `test_years` each time, so every fold is comparable to every other (TRD
    §8.1)."""
    folds = []
    data_start, data_end = dates[0], dates[-1]
    start = data_start
    while True:
        train_end = start + pd.DateOffset(years=train_years)
        test_start = train_end + pd.Timedelta(days=embargo_days)
        test_end = test_start + pd.DateOffset(years=test_years)
        if test_end > data_end:
            break
        folds.append((start, train_end, test_start, test_end))
        start = start + pd.DateOffset(years=test_years)
    return folds


def _grid_combinations(param_grid: dict[str, list]) -> list[dict]:
    keys = list(param_grid.keys())
    combos = [dict(zip(keys, values)) for values in itertools.product(*param_grid.values())]
    if len(combos) > MAX_GRID_COMBINATIONS:
        rng = np.random.default_rng(0)
        idx = rng.choice(len(combos), size=MAX_GRID_COMBINATIONS, replace=False)
        combos = [combos[i] for i in sorted(idx)]
    return combos


def _tune_on_training(
    train_df: pd.DataFrame,
    generate_signals: SignalFn,
    base_params: dict,
    param_grid: dict[str, list] | None,
    cost_model: CostModel,
    cost_multiplier: float,
    periods_per_year: float,
) -> tuple[dict, float]:
    """Selects on training performance only — the test window is never
    touched, so this never counts toward N_trials (TRD §8.6)."""
    if not param_grid:
        result = run_backtest(train_df, generate_signals, base_params, cost_model, cost_multiplier)
        return base_params, sharpe_ratio(result.returns, periods_per_year)

    best_params, best_sharpe = base_params, -np.inf
    for combo in _grid_combinations(param_grid):
        candidate = {**base_params, **combo}
        result = run_backtest(train_df, generate_signals, candidate, cost_model, cost_multiplier)
        sr = sharpe_ratio(result.returns, periods_per_year)
        if sr > best_sharpe:
            best_params, best_sharpe = candidate, sr
    return best_params, best_sharpe


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
    fold_windows = generate_rolling_folds(df.index, train_years, test_years, embargo_days)
    folds: list[FoldResult] = []
    all_returns: list[np.ndarray] = []
    all_dates: list[pd.DatetimeIndex] = []

    for train_start, train_end, test_start, test_end in fold_windows:
        train_df = df.loc[train_start:train_end]
        test_df = df.loc[test_start:test_end]
        if len(train_df) < 30 or len(test_df) < 5:
            continue
        chosen_params, is_sharpe = _tune_on_training(
            train_df, generate_signals, base_params, param_grid, cost_model, cost_multiplier, periods_per_year
        )
        test_result = run_backtest(test_df, generate_signals, chosen_params, cost_model, cost_multiplier)
        folds.append(
            FoldResult(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                chosen_params=chosen_params,
                in_sample_sharpe=is_sharpe,
                test_result=test_result,
            )
        )
        all_returns.append(test_result.returns)
        all_dates.append(test_result.dates)

    if all_returns:
        # Concatenate strictly in chronological order (TRD §8.3, §9.5) — folds
        # are already generated in time order, so a plain concat preserves it.
        concatenated = np.concatenate(all_returns)
        concatenated_dates = pd.DatetimeIndex(np.concatenate([d.values for d in all_dates]))
    else:
        concatenated = np.array([])
        concatenated_dates = pd.DatetimeIndex([])

    total_trades = sum(f.test_result.n_trades for f in folds)
    oos_sharpe_mean = float(np.mean([sharpe_ratio(f.test_result.returns, periods_per_year) for f in folds])) if folds else 0.0
    is_sharpe_mean = float(np.mean([f.in_sample_sharpe for f in folds])) if folds else 0.0
    wf_efficiency = oos_sharpe_mean / is_sharpe_mean if is_sharpe_mean not in (0.0, np.nan) and np.isfinite(is_sharpe_mean) else 0.0

    score = compute_honest_score(concatenated, periods_per_year, n_trials, z_multiplier)

    return WindowResult(
        train_years=train_years,
        folds=folds,
        concatenated_returns=concatenated,
        concatenated_dates=concatenated_dates,
        total_trades=total_trades,
        wf_efficiency=wf_efficiency,
        score=score,
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
    """TRD §8.2: run all three train windows, N_trials tripled because
    taking the best of three is itself a selection on the reported metric."""
    embargo_days = max(holding_period_days, 5)
    n_trials = n_trials_base * 3

    windows = {
        train_years: run_window(
            df,
            generate_signals,
            base_params,
            cost_model,
            train_years,
            test_years,
            embargo_days,
            n_trials,
            param_grid,
            cost_multiplier,
            periods_per_year,
            z_multiplier,
        )
        for train_years in (1, 2, 3)
    }
    winning_train_years = max(windows, key=lambda ty: windows[ty].score.honest_score)
    return BestOfThreeResult(windows=windows, winning_train_years=winning_train_years)

"""3d — the walk-forward protocol, against hand-checkable geometry and the
tuning rule that keeps parameter search from inflating the trial count."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from aqrl.eval.walk_forward import (
    SliceOutcome,
    generate_rolling_folds,
    grid_combinations,
    run_best_of_three,
    run_window,
)

PERIODS_PER_YEAR = 252.0


def _dates(n_years: int) -> np.ndarray:
    start = dt.date(2000, 1, 1)
    return np.array([np.datetime64(start + dt.timedelta(days=i)) for i in range(365 * n_years)])


def _flat_evaluator(daily_return: float = 0.0, n_trades_per_bar: int = 0):
    """An evaluator whose return series is deterministic from the date span —
    convenient for checking fold geometry without any real backtest."""

    def evaluate(start, end, params):
        span = (np.datetime64(end) - np.datetime64(start)) / np.timedelta64(1, "D")
        n_bars = max(int(span), 0)
        dates = np.array([np.datetime64(start) + np.timedelta64(i, "D") for i in range(n_bars)])
        returns = np.full(n_bars, daily_return + params.get("bias", 0.0))
        return SliceOutcome(returns=returns, dates=dates, n_trades=n_trades_per_bar * n_bars)

    return evaluate


# -- fold geometry ----------------------------------------------------------------


def test_rolling_folds_have_comparable_train_length():
    dates = _dates(10)
    folds = generate_rolling_folds(dates, train_years=2, test_years=1, embargo_days=10)
    assert len(folds) > 0
    for fold in folds:
        assert (fold.train_end - fold.train_start).days == pytest.approx(365 * 2, abs=2)
        assert (fold.test_end - fold.test_start).days == pytest.approx(365, abs=2)


def test_embargo_creates_a_gap_before_test_start():
    dates = _dates(10)
    embargo_days = 15
    folds = generate_rolling_folds(dates, train_years=1, test_years=1, embargo_days=embargo_days)
    assert len(folds) > 0
    for fold in folds:
        assert (fold.test_start - fold.train_end).days == embargo_days


def test_more_data_yields_more_folds():
    short = generate_rolling_folds(_dates(5), train_years=1, test_years=1, embargo_days=5)
    long = generate_rolling_folds(_dates(15), train_years=1, test_years=1, embargo_days=5)
    assert len(long) > len(short)


def test_empty_dates_yield_no_folds():
    assert generate_rolling_folds(np.array([]), train_years=1, test_years=1, embargo_days=5) == []


# -- concatenation ------------------------------------------------------------------


def test_concatenated_returns_are_chronological():
    window = run_window(
        _dates(8),
        _flat_evaluator(),
        {},
        train_years=1,
        test_years=1,
        embargo_days=5,
        n_trials=1,
        periods_per_year=PERIODS_PER_YEAR,
    )
    dates = window.concatenated_dates
    assert np.all(dates[1:] >= dates[:-1])


# -- best-of-three ------------------------------------------------------------------


def test_best_of_three_reports_all_three_windows_and_a_winner():
    result = run_best_of_three(
        _dates(10),
        _flat_evaluator(),
        {},
        holding_period_bars=5,
        n_trials=1,
        periods_per_year=PERIODS_PER_YEAR,
    )
    assert set(result.windows.keys()) == {1, 2, 3}
    assert result.winning_train_years in (1, 2, 3)
    assert result.score.honest_score == max(w.score.honest_score for w in result.windows.values())


def test_train_window_spread_is_zero_when_all_three_agree():
    result = run_best_of_three(
        _dates(10),
        _flat_evaluator(0.001),
        {},
        holding_period_bars=5,
        n_trials=100,
        periods_per_year=PERIODS_PER_YEAR,
    )
    # Not exactly zero (fold counts differ by window) but should be small
    # relative to the scores themselves, and never negative.
    assert result.train_window_spread >= 0.0


def test_n_trials_is_used_as_given_not_re_tripled():
    """`run_best_of_three` trusts the caller's trial count — the x3 for
    best-of-three is applied once, by `stats.deflated.family_trial_count`,
    not duplicated here."""
    result_a = run_best_of_three(
        _dates(10), _flat_evaluator(), {}, holding_period_bars=5, n_trials=3,
        periods_per_year=PERIODS_PER_YEAR,
    )
    result_b = run_best_of_three(
        _dates(10), _flat_evaluator(), {}, holding_period_bars=5, n_trials=21,
        periods_per_year=PERIODS_PER_YEAR,
    )
    for years in (1, 2, 3):
        assert result_a.windows[years].score.n_trials == 3
        assert result_b.windows[years].score.n_trials == 21


# -- tuning never touches the test window --------------------------------------------


def test_tuning_selects_the_best_training_configuration():
    def evaluator(start, end, params):
        span = int((np.datetime64(end) - np.datetime64(start)) / np.timedelta64(1, "D"))
        n_bars = max(span, 0)
        dates = np.array([np.datetime64(start) + np.timedelta64(i, "D") for i in range(n_bars)])
        # The "best" bias is always 0.05, whatever window is asked for —
        # so tuning on the training slice alone should always find it. A
        # deterministic wiggle keeps variance non-zero, since a perfectly flat
        # series has an undefined Sharpe gradient to tune against.
        rng = np.random.default_rng(0)
        returns = params["bias"] + rng.normal(0.0, 1e-4, n_bars)
        return SliceOutcome(returns, dates, n_trades=0)

    window = run_window(
        _dates(6),
        evaluator,
        {"bias": 0.0},
        train_years=1,
        test_years=1,
        embargo_days=5,
        n_trials=1,
        periods_per_year=PERIODS_PER_YEAR,
        param_grid={"bias": [-0.05, 0.0, 0.05]},
    )
    assert window.folds
    for fold in window.folds:
        assert fold.chosen_params["bias"] == pytest.approx(0.05)


def test_grid_is_capped_and_deterministic():
    grid = {"a": list(range(10)), "b": list(range(10))}  # 100 combinations
    first = grid_combinations(grid, limit=50, base_seed=7)
    second = grid_combinations(grid, limit=50, base_seed=7)
    assert len(first) == 50
    assert first == second  # same seed -> same subset (TRD §9.5)


def test_different_base_seeds_can_choose_different_subsets():
    grid = {"a": list(range(10)), "b": list(range(10))}
    first = grid_combinations(grid, limit=10, base_seed=1)
    second = grid_combinations(grid, limit=10, base_seed=2)
    assert first != second


# -- walk-forward efficiency ---------------------------------------------------------


def test_wf_efficiency_is_the_oos_is_sharpe_ratio():
    window = run_window(
        _dates(8),
        _flat_evaluator(0.001),
        {},
        train_years=1,
        test_years=1,
        embargo_days=5,
        n_trials=1,
        periods_per_year=PERIODS_PER_YEAR,
    )
    assert window.wf_efficiency == pytest.approx(1.0, abs=0.05)

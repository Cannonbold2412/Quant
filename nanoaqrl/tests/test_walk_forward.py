import pandas as pd
import pytest

from nanoaqrl._lib.cost_models import NSE_CASH_EQUITY
from nanoaqrl._lib.synthetic_data import synthetic_ohlcv
from nanoaqrl._lib.walk_forward import generate_rolling_folds, run_best_of_three, run_window


def _flat_signal(df, params):
    return pd.Series(1.0, index=df.index)


def test_rolling_folds_are_comparable_length():
    """Rolling (not anchored): every fold's train length is identical (TRD §8.1)."""
    dates = pd.bdate_range("2000-01-01", periods=252 * 10)
    folds = generate_rolling_folds(dates, train_years=2, test_years=1, embargo_days=10)
    assert len(folds) > 0
    for train_start, train_end, test_start, test_end in folds:
        assert (train_end - train_start).days == pytest.approx(365 * 2, abs=2)
        assert (test_end - test_start).days == pytest.approx(365, abs=2)


def test_embargo_creates_a_gap_before_test_start():
    dates = pd.bdate_range("2000-01-01", periods=252 * 10)
    embargo_days = 15
    folds = generate_rolling_folds(dates, train_years=1, test_years=1, embargo_days=embargo_days)
    for train_start, train_end, test_start, test_end in folds:
        assert (test_start - train_end).days == embargo_days


def test_fold_count_increases_with_more_data():
    short = pd.bdate_range("2000-01-01", periods=252 * 5)
    long = pd.bdate_range("2000-01-01", periods=252 * 15)
    folds_short = generate_rolling_folds(short, train_years=1, test_years=1, embargo_days=5)
    folds_long = generate_rolling_folds(long, train_years=1, test_years=1, embargo_days=5)
    assert len(folds_long) > len(folds_short)


def test_concatenated_returns_are_in_chronological_order():
    df = synthetic_ohlcv(252 * 8, seed=1)
    window = run_window(
        df, _flat_signal, {}, NSE_CASH_EQUITY,
        train_years=1, test_years=1, embargo_days=5, n_trials=1,
    )
    dates = window.concatenated_dates
    assert dates.is_monotonic_increasing


def test_best_of_three_reports_all_three_windows_and_a_winner():
    df = synthetic_ohlcv(252 * 10, seed=2)
    result = run_best_of_three(
        df, _flat_signal, {}, NSE_CASH_EQUITY,
        holding_period_days=5, n_trials_base=1,
    )
    assert set(result.windows.keys()) == {1, 2, 3}
    assert result.winning_train_years in (1, 2, 3)
    assert result.score.honest_score == max(w.score.honest_score for w in result.windows.values())


def test_best_of_three_n_trials_is_tripled_vs_base():
    df = synthetic_ohlcv(252 * 10, seed=3)
    result = run_best_of_three(
        df, _flat_signal, {}, NSE_CASH_EQUITY,
        holding_period_days=5, n_trials_base=7,
    )
    for window in result.windows.values():
        assert window.score.n_trials == 21

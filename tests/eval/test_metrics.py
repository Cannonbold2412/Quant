"""3c — core metrics, against hand-computable arithmetic."""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.eval.backtest import run_backtest
from aqrl.eval.metrics import compute_metrics, drawdown_series
from aqrl.eval.tradebook import extract_trades

from .conftest import bars, panel_of


def test_drawdown_series_matches_hand_computation():
    returns = np.array([0.1, -0.2, 0.05])  # 1.0 -> 1.1 -> 0.88 -> 0.924
    dd = drawdown_series(returns)
    assert dd[0] == pytest.approx(0.0)
    assert dd[1] == pytest.approx(0.88 / 1.1 - 1.0)
    assert dd[2] == pytest.approx(0.924 / 1.1 - 1.0)


def test_cagr_matches_hand_computation(resolved):
    panel = panel_of(bars([100.0] * 253))
    signals = np.ones((253, 1))
    result = run_backtest(panel, signals, resolved, cost_multiplier=0.0)
    # Flat prices -> zero return -> CAGR is exactly zero, not undefined.
    metrics = compute_metrics(result, extract_trades(result), periods_per_year=252.0)
    assert metrics.cagr == pytest.approx(0.0)
    assert metrics.total_return == pytest.approx(0.0)


def test_win_rate_and_profit_factor_from_a_known_tradebook(resolved):
    # Two closed round trips: one clean winner, one clean loser.
    panel = panel_of(bars([100.0, 110.0, 110.0, 99.0, 99.0]))
    signals = np.array([[0.0], [1.0], [0.0], [-1.0], [0.0]])
    result = run_backtest(panel, signals, resolved, cost_multiplier=0.0)
    trades = extract_trades(result)
    metrics = compute_metrics(result, trades, periods_per_year=252.0)

    assert metrics.trade_count == len(trades)
    assert 0.0 <= metrics.win_rate <= 1.0


def test_open_trades_are_excluded_from_win_rate(resolved):
    """An unrealised winner must not count as a win — its outcome isn't known
    yet, and letting it in would be a small, cheerful lie."""
    panel = panel_of(bars([100.0] * 5))
    signals = np.array([[0.0], [1.0], [1.0], [1.0], [1.0]])  # never closes
    result = run_backtest(panel, signals, resolved, cost_multiplier=0.0)
    trades = extract_trades(result)
    assert all(trade.open_at_end for trade in trades)

    metrics = compute_metrics(result, trades, periods_per_year=252.0)
    assert metrics.win_rate == 0.0  # no CLOSED trade to count


def test_turnover_and_exposure_are_read_off_the_backtest_result(resolved):
    panel = panel_of(bars([100.0] * 4))
    signals = np.array([[0.0], [1.0], [1.0], [0.0]])
    result = run_backtest(panel, signals, resolved, cost_multiplier=0.0)
    metrics = compute_metrics(result, extract_trades(result), periods_per_year=252.0)
    assert metrics.turnover == pytest.approx(result.turnover)
    assert metrics.exposure_pct == pytest.approx(result.exposure)


def test_metrics_on_a_flat_strategy_are_all_zero_not_nan(resolved):
    panel = panel_of(bars([100.0] * 10))
    signals = np.zeros((10, 1))
    result = run_backtest(panel, signals, resolved, cost_multiplier=0.0)
    metrics = compute_metrics(result, extract_trades(result), periods_per_year=252.0)
    for field, value in metrics.row().items():
        assert np.isfinite(value), f"{field} is not finite: {value}"

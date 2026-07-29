"""Regime labelling and per-regime performance."""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.eval.regimes import REGIMES, label_regimes, market_returns, regime_performance
from aqrl.eval.panel import PricePanel

from .conftest import bars


def test_market_returns_are_equal_weighted_across_listed_instruments():
    frame = bars({"AAA": [100.0, 110.0], "BBB": [100.0, 90.0]})
    panel = PricePanel.from_frame(frame)
    returns = market_returns(panel)
    assert returns[1] == pytest.approx(np.mean([0.1, -0.1]))


def test_a_crash_is_labelled_crisis():
    rng = np.random.default_rng(0)
    calm = 0.0003 + rng.normal(0.0, 0.005, 300)
    crash = np.full(30, -0.03)  # a fast, deep drawdown
    returns = np.concatenate([calm, crash])
    labels = label_regimes(returns, window=63)
    assert "crisis" in labels[-30:]


def test_all_labels_are_from_the_known_set():
    rng = np.random.default_rng(1)
    returns = rng.normal(0.0005, 0.01, 400)
    labels = label_regimes(returns)
    assert set(labels) <= set(REGIMES)


def test_short_series_defaults_to_sideways_rather_than_crashing():
    labels = label_regimes(np.array([0.01, -0.01, 0.02]))
    assert list(labels) == ["sideways", "sideways", "sideways"]


def test_regime_performance_reports_only_regimes_actually_seen():
    rng = np.random.default_rng(2)
    returns = rng.normal(0.0005, 0.005, 200)
    dates = np.array([np.datetime64("2020-01-01") + np.timedelta64(i, "D") for i in range(200)])
    labels = np.full(200, "trending", dtype=object)

    slices = regime_performance(returns, dates, labels, periods_per_year=252.0)
    assert len(slices) == 1
    assert slices[0].regime == "trending"
    assert slices[0].n_bars == 200


def test_regime_performance_handles_a_zero_bar_regime_gracefully():
    returns = np.array([0.01, -0.02, 0.03])
    dates = np.array(["2020-01-01", "2020-01-02", "2020-01-03"], dtype="datetime64[D]")
    labels = np.array(["trending", "trending", "trending"], dtype=object)
    slices = regime_performance(returns, dates, labels, periods_per_year=252.0)
    assert all(np.isfinite(s.sharpe) for s in slices)
    assert all(np.isfinite(s.max_drawdown) for s in slices)

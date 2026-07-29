"""Known-answer tests — is each operator computing the thing it claims to?

The registry-wide suites prove operators are *well-behaved*: causal,
deterministic, correctly warmed up. None of that proves they are *correct*. An
EMA with the wrong alpha is perfectly causal and perfectly deterministic, and
perfectly wrong — and a wrong indicator does not announce itself, it just
quietly changes what the lab discovers.

So each operator here is checked against a closed form, a worked example, or a
reference implementation whose answer is known before the test runs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aqrl.operators import get
from aqrl.operators._windows import (
    rolling_max,
    rolling_mean,
    rolling_min,
    rolling_std,
    shift,
    true_range,
    wilder_smooth,
)


@pytest.fixture
def prices() -> np.ndarray:
    rng = np.random.default_rng(7)
    return 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.012, 600))


@pytest.fixture
def ohlc(prices):
    return {"series": prices, "close": prices, "high": prices * 1.008, "low": prices * 0.991}


# -- windowing primitives --------------------------------------------------------


@pytest.mark.parametrize("window", [2, 20, 100])
def test_rolling_mean_matches_pandas(prices, window):
    expected = pd.Series(prices).rolling(window).mean().to_numpy()
    assert np.allclose(rolling_mean(prices, window), expected, equal_nan=True, atol=1e-9)


@pytest.mark.parametrize("window", [2, 20, 100])
def test_rolling_std_matches_pandas(prices, window):
    expected = pd.Series(prices).rolling(window).std().to_numpy()
    assert np.allclose(rolling_std(prices, window), expected, equal_nan=True, atol=1e-7)


@pytest.mark.parametrize("window", [2, 20, 100])
def test_rolling_extremes_match_pandas_exactly(prices, window):
    series = pd.Series(prices)
    assert np.array_equal(rolling_max(prices, window), series.rolling(window).max(), equal_nan=True)
    assert np.array_equal(rolling_min(prices, window), series.rolling(window).min(), equal_nan=True)


def test_a_leading_nan_does_not_poison_the_whole_series():
    """The bug a bare `cumsum` would reintroduce.

    Operators compose — a rolling mean of an ATR is routine — so a leading NaN
    is the normal case. A window containing a NaN is NaN; every window after it
    is not.
    """
    values = np.array([np.nan, np.nan, 1.0, 2.0, 3.0, 4.0, 5.0])
    result = rolling_mean(values, 3)
    assert np.isnan(result[:4]).all()
    assert result[4] == pytest.approx(2.0)   # (1+2+3)/3
    assert result[6] == pytest.approx(4.0)   # (3+4+5)/3


def test_shift_refuses_to_look_forward():
    with pytest.raises(ValueError, match="future values backwards"):
        shift(np.arange(10.0), -1)


def test_true_range_worked_example():
    """max(H-L, |H-C_prev|, |L-C_prev|) on a gap-up bar."""
    high = np.array([10.0, 15.0])
    low = np.array([9.0, 14.0])
    close = np.array([9.5, 14.5])
    result = true_range(high, low, close)
    assert np.isnan(result[0])          # no prior close on the first bar
    # H-L = 1; |15-9.5| = 5.5; |14-9.5| = 4.5 -> the gap dominates.
    assert result[1] == pytest.approx(5.5)


def test_wilder_smoothing_is_not_an_ema():
    """Wilder's alpha is 1/n; an EMA of the same nominal period uses 2/(n+1).

    Conflating them is a classic silent error: ATR(14) computed as EMA(14) is
    roughly a 7-period Wilder, so every ATR-based stop is tighter than intended.
    """
    values = np.arange(1.0, 21.0)
    period = 5
    smoothed = wilder_smooth(values, period)

    seed = float(np.mean(values[:period]))
    assert smoothed[period - 1] == pytest.approx(seed)
    expected = (seed * (period - 1) + values[period]) / period
    assert smoothed[period] == pytest.approx(expected)

    ema_alpha = 2.0 / (period + 1)
    ema_next = ema_alpha * values[period] + (1 - ema_alpha) * seed
    assert smoothed[period] != pytest.approx(ema_next)


# -- transformations ---------------------------------------------------------------


def test_ema_follows_the_closed_form(prices):
    span = 20
    result = get("ema")({"series": prices}, span=span)

    alpha = 2.0 / (span + 1.0)
    expected = float(np.mean(prices[:span]))
    assert result[span - 1] == pytest.approx(expected)
    for index in range(span, span + 25):
        expected = alpha * prices[index] + (1 - alpha) * expected
        assert result[index] == pytest.approx(expected)


def test_ema_of_a_constant_is_that_constant():
    flat = np.full(100, 42.0)
    result = get("ema")({"series": flat}, span=10)
    assert np.allclose(result[9:], 42.0)


def test_zscore_of_a_flat_window_is_zero_not_infinity():
    flat = np.full(60, 5.0)
    result = get("zscore")({"series": flat}, window=20)
    assert np.isnan(result[:19]).all()
    assert np.allclose(result[19:], 0.0)


def test_zscore_matches_the_definition(prices):
    window = 30
    result = get("zscore")({"series": prices}, window=window)
    expected = (prices - rolling_mean(prices, window)) / rolling_std(prices, window)
    assert np.allclose(result[window:], expected[window:], atol=1e-9)


def test_atr_matches_wilder_on_true_range(ohlc):
    period = 14
    result = get("atr")(ohlc, period=period)
    expected = wilder_smooth(true_range(ohlc["high"], ohlc["low"], ohlc["close"]), period)
    assert np.allclose(result, expected, equal_nan=True)


def test_frac_diff_weights_are_the_binomial_series():
    """w[0]=1, w[k] = -w[k-1]*(d-k+1)/k. At d=1 this must reduce to a first
    difference: weights [1, -1] and nothing more."""
    operator = get("frac_diff")
    weights = operator.weights(1.0, 1e-8, 200)
    assert weights[0] == pytest.approx(1.0)
    assert weights[1] == pytest.approx(-1.0)
    assert len(weights) == 2, "d=1 must terminate at the plain first difference"

    half = operator.weights(0.5, 1e-8, 200)
    assert half[0] == pytest.approx(1.0)
    assert half[1] == pytest.approx(-0.5)
    assert half[2] == pytest.approx(-0.125)


def test_frac_diff_at_d_one_is_a_first_difference(prices):
    result = get("frac_diff")({"series": prices}, d=1.0, threshold=1e-8, max_width=200)
    expected = np.diff(prices, prepend=np.nan)
    assert np.allclose(result[1:], expected[1:], atol=1e-9)


def test_frac_diff_at_d_zero_is_the_identity(prices):
    result = get("frac_diff")({"series": prices}, d=0.0, threshold=1e-8, max_width=200)
    assert np.allclose(result, prices, atol=1e-9)


def test_kalman_with_tiny_process_variance_barely_moves():
    """Q -> 0 means "the state never changes", so the filter should flatten."""
    rng = np.random.default_rng(2)
    noisy = 50.0 + rng.normal(0, 1.0, 400)
    result = get("kalman")({"series": noisy}, process_var=1e-12, observation_var=1.0)
    assert float(np.std(result[50:])) < float(np.std(noisy[50:])) / 5


def test_kalman_with_huge_process_variance_tracks_the_input():
    rng = np.random.default_rng(2)
    noisy = 50.0 + rng.normal(0, 1.0, 200)
    result = get("kalman")({"series": noisy}, process_var=10.0, observation_var=1e-9)
    assert np.allclose(result[1:], noisy[1:], atol=1e-3)


def test_roc_is_the_fractional_change(prices):
    result = get("roc")({"series": prices}, period=5)
    expected = prices[5:] / prices[:-5] - 1.0
    assert np.allclose(result[5:], expected, atol=1e-12)


def test_percent_rank_is_one_at_a_new_high():
    rising = np.arange(1.0, 51.0)
    result = get("percent_rank")({"series": rising}, window=10)
    assert np.allclose(result[9:], 1.0)


def test_rolling_pca_of_a_pure_trend_is_finite(prices):
    result = get("rolling_pca")({"series": prices}, window=60, lags=3)
    assert np.isfinite(result[62:]).all()


# -- signals -----------------------------------------------------------------------


def test_crossover_respects_the_deadband():
    fast = np.array([100.0, 101.0, 100.0, 99.0])
    slow = np.array([100.0, 100.0, 100.0, 100.0])
    result = get("crossover")({"fast": fast, "slow": slow}, min_gap_pct=0.005)
    # gaps: 0, +1%, 0, -1% against a 0.5% deadband.
    assert list(result) == [0.0, 1.0, 0.0, -1.0]


def test_breakout_uses_the_prior_window_not_the_current_bar():
    """Including the current bar makes the test trivially true on any new high."""
    high = np.array([10.0, 10.0, 10.0, 10.0, 12.0])
    low = np.array([9.0, 9.0, 9.0, 9.0, 9.0])
    close = np.array([9.5, 9.5, 9.5, 9.5, 11.5])
    result = get("breakout")({"high": high, "low": low, "close": close}, lookback=3)
    # Bar 4's close of 11.5 exceeds the prior 3-bar high of 10.
    assert result[4] == 1.0
    assert result[3] == 0.0


def test_mean_reversion_fades_the_extreme():
    """Stretched UP must be a SHORT — the one sign convention worth asserting."""
    series = np.concatenate([np.full(40, 10.0), [10.0], np.full(1, 100.0)])
    result = get("mean_reversion")({"series": series}, window=20, entry_z=1.5)
    assert result[-1] == -1.0


def test_volume_confirmation_needs_a_multiple_of_average():
    volume = np.concatenate([np.full(30, 100.0), [400.0]])
    result = get("volume_confirmation")({"volume": volume}, window=20, multiple=1.5)
    assert result[-1] == 1.0
    assert result[-2] == 0.0


def test_and_is_commutative_in_value_not_just_in_hash():
    a = np.array([1.0, 1.0, -1.0, -1.0, 0.0])
    b = np.array([1.0, -1.0, -1.0, 1.0, 1.0])
    forward = get("and")({"a": a, "b": b})
    backward = get("and")({"a": b, "b": a})
    assert np.array_equal(forward, backward, equal_nan=True)
    assert list(forward) == [1.0, 0.0, -1.0, 0.0, 0.0]


def test_or_cancels_opposing_directions():
    a = np.array([1.0, 1.0, 0.0])
    b = np.array([-1.0, 0.0, -1.0])
    assert list(get("or")({"a": a, "b": b})) == [0.0, 1.0, -1.0]


# -- risk --------------------------------------------------------------------------


def test_time_stop_flattens_after_the_limit():
    position = np.ones(10)
    result = get("time_stop")({"position": position}, max_bars=3)
    # Bars 0..2 are held (entry plus two), bar 3 exceeds the limit.
    assert list(result[:5]) == [1.0, 1.0, 1.0, 0.0, 1.0]


def test_atr_stop_exits_when_price_falls_through_the_level():
    n = 60
    close = np.concatenate([np.full(40, 100.0), np.linspace(100.0, 60.0, n - 40)])
    inputs = {
        "position": np.ones(n),
        "close": close,
        "high": close * 1.01,
        "low": close * 0.99,
    }
    result = get("atr_stop")(inputs, period=14, multiple=2.0)
    assert np.isnan(result[:14]).all(), "no position before the stop can be computed"
    assert (result[45:] == 0.0).any(), "a 40% decline must trip a 2-ATR stop"


def test_trailing_stop_only_ever_tightens():
    """Ratchets with price; a whole-series cummax would know future highs."""
    n = 80
    close = np.concatenate([np.linspace(100.0, 150.0, 60), np.linspace(150.0, 120.0, 20)])
    inputs = {
        "position": np.ones(n),
        "close": close,
        "high": close * 1.005,
        "low": close * 0.995,
    }
    result = get("trailing_stop")(inputs, period=14, multiple=2.0)
    assert (result[60:] == 0.0).any(), "the pullback must trip a trailing stop"


def test_kelly_is_mu_over_sigma_squared(prices):
    window, fraction, cap = 100, 1.0, 10.0
    returns = np.diff(prices, prepend=np.nan) / np.concatenate(([np.nan], prices[:-1]))
    result = get("kelly")(
        {"position": np.ones(len(prices)), "returns": returns},
        window=window,
        fraction=fraction,
        cap=cap,
    )
    mean = rolling_mean(returns, window)
    std = rolling_std(returns, window)
    expected = np.abs(np.clip(mean / (std * std), -cap, cap))
    assert np.allclose(result[window + 1 :], expected[window + 1 :], atol=1e-9)


def test_vol_target_scales_toward_the_target():
    """Doubling realised volatility must halve the position."""
    n = 400
    rng = np.random.default_rng(5)
    quiet = rng.normal(0, 0.01, n)
    loud = rng.normal(0, 0.02, n)
    operator = get("vol_target")
    quiet_size = operator({"position": np.ones(n), "returns": quiet}, window=60,
                          target_vol=0.01, max_leverage=20.0)
    loud_size = operator({"position": np.ones(n), "returns": loud}, window=60,
                         target_vol=0.01, max_leverage=20.0)
    assert np.nanmean(quiet_size[100:]) > 1.5 * np.nanmean(loud_size[100:])


def test_vol_target_respects_the_leverage_cap():
    n = 300
    almost_flat = np.full(n, 1e-9)
    result = get("vol_target")(
        {"position": np.ones(n), "returns": almost_flat},
        window=60, target_vol=0.15, max_leverage=3.0,
    )
    assert np.nanmax(result) <= 3.0 + 1e-12


# -- portfolio -----------------------------------------------------------------------


def test_equal_weight_is_one_over_n():
    rng = np.random.default_rng(1)
    panel = rng.normal(0, 0.01, (50, 4))
    weights = get("equal_weight")({"returns": panel})
    assert np.allclose(weights, 0.25)
    assert np.allclose(weights.sum(axis=1), 1.0)


def test_risk_parity_underweights_the_volatile_asset():
    """Weight proportional to 1/sigma — the analytic two-asset case."""
    n, window = 300, 60
    rng = np.random.default_rng(4)
    panel = np.column_stack([rng.normal(0, 0.01, n), rng.normal(0, 0.04, n)])
    weights = get("risk_parity")({"returns": panel}, window=window)

    final = weights[-1]
    assert np.allclose(final.sum(), 1.0)
    assert final[0] > final[1], "the quiet asset must carry the larger weight"
    # sigma ratio is ~4x, so the weight ratio should be ~4x as well.
    assert 2.5 < final[0] / final[1] < 6.0


def test_correlation_cluster_splits_the_budget_across_clusters():
    """Two correlated names plus one independent must not become 1/3 each.

    The whole point: naive 1/N gives the two-name theme 2/3 of the risk budget
    for no reason other than ticker count.
    """
    n = 400
    rng = np.random.default_rng(6)
    shared = rng.normal(0, 0.01, n)
    panel = np.column_stack(
        [
            shared + rng.normal(0, 0.0005, n),
            shared + rng.normal(0, 0.0005, n),
            rng.normal(0, 0.01, n),
        ]
    )
    weights = get("correlation_cluster")({"returns": panel}, window=120, n_clusters=2)
    final = weights[-1]
    assert np.allclose(final.sum(), 1.0)
    # The lone independent name gets a whole cluster's half.
    assert final[2] == pytest.approx(0.5)
    assert final[0] == pytest.approx(0.25)
    assert final[1] == pytest.approx(0.25)

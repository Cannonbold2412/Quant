"""3b — P0: smoke, correctness, look-ahead, leakage and data provenance.

The known-answer cases mandated by Implementation_Plan §5.2 live here at the
unit level; `test_known_answers.py` re-runs the mandatory two (look-ahead,
leaky-vectorised) through the full engine as the M1 acceptance gate.
"""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.eval.p0 import (
    empirical_leakage_scan_arrays,
    snapshot_provenance_checks,
    spec_absolute_level_scan,
    static_lookahead_scan,
)
from aqrl.operators.spec import Node, StrategySpec
from aqrl.profiles import ProfileLoader

LOOKAHEAD_SOURCE = """
def generate_signals(df, params):
    future = df['close'].shift(-1)
    return (future > df['close']).astype(float)
"""

LEAKY_VECTORISED_SOURCE = """
def generate_signals(df, params):
    mean = df['close'].mean()
    std = df['close'].std()
    z = (df['close'] - mean) / std
    return (z > 0).astype(float)
"""

BACKFILL_SOURCE = """
def generate_signals(df, params):
    x = df['close'].pct_change().fillna(method='bfill')
    return (x > 0).astype(float)
"""

CENTRED_WINDOW_SOURCE = """
def generate_signals(df, params):
    m = df['close'].rolling(20, center=True).mean()
    return (df['close'] > m).astype(float)
"""

LEGITIMATE_SOURCE = """
def generate_signals(df, params):
    fast = df['close'].rolling(10).mean()
    slow = df['close'].rolling(50).mean()
    return (fast > slow).astype(float)
"""


# -- P0a: static scan --------------------------------------------------------------


def test_catches_shift_negative():
    violations = static_lookahead_scan(LOOKAHEAD_SOURCE)
    assert violations
    assert any("shift(-n)" in v for v in violations)


def test_catches_whole_series_vectorised_leak():
    """Mandatory per TRD §9.4: vectorisation is the top source of look-ahead."""
    violations = static_lookahead_scan(LEAKY_VECTORISED_SOURCE)
    assert violations
    assert any("whole-series" in v for v in violations)


def test_catches_backfill():
    violations = static_lookahead_scan(BACKFILL_SOURCE)
    assert violations
    assert any("forbidden" in v for v in violations)


def test_catches_centred_windows():
    violations = static_lookahead_scan(CENTRED_WINDOW_SOURCE)
    assert violations
    assert any("centred" in v for v in violations)


def test_passes_a_legitimate_strategy():
    assert static_lookahead_scan(LEGITIMATE_SOURCE) == []


# -- P0b: empirical truncation invariance -------------------------------------------


def test_empirical_scan_catches_a_leak_the_static_scan_misses():
    """`.iloc[-1]`-style leaks trip no AST pattern — only truncation
    invariance catches them."""
    rng = np.random.default_rng(0)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, 300))

    def leaky_last_value_signal(columns):
        last_close = columns["close"][-1]
        return (columns["close"] > last_close).astype(float)

    assert static_lookahead_scan(
        "def generate_signals(df, params):\n"
        "    last_close = df['close'].iloc[-1]\n"
        "    return (df['close'] > last_close).astype(float)\n"
    ) == []

    violations = empirical_leakage_scan_arrays(leaky_last_value_signal, {"close": close})
    assert violations


def test_empirical_scan_passes_a_legitimate_rolling_signal():
    rng = np.random.default_rng(1)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, 300))

    def rolling_signal(columns):
        series = columns["close"]
        out = np.full(series.size, np.nan)
        for i in range(20, series.size):
            out[i] = 1.0 if series[i] > series[i - 20 : i].mean() else -1.0
        return out

    assert empirical_leakage_scan_arrays(rolling_signal, {"close": close}) == []


# -- P0c: absolute price levels ------------------------------------------------------


def test_flags_a_threshold_directly_on_a_raw_price():
    spec = StrategySpec(
        entry_logic=[
            Node(id="e1", operator="threshold", inputs={"series": "price.close"},
                 params={"upper": 500.0, "lower": -500.0})
        ]
    )
    violations = spec_absolute_level_scan(spec)
    assert violations
    assert "e1" in violations[0]


def test_permits_a_threshold_on_a_unit_free_transform():
    spec = StrategySpec(
        entry_logic=[
            Node(id="z", operator="zscore", inputs={"series": "price.close"}),
            Node(id="e1", operator="threshold", inputs={"series": "z"},
                 params={"upper": 1.0, "lower": -1.0}),
        ]
    )
    assert spec_absolute_level_scan(spec) == []


def test_permits_a_threshold_on_a_rolling_transform_of_price_but_still_flags_it():
    """`rolling_mean` is unit-free in the sense of "not itself a raw source",
    but it stays in rupees — so this is exactly the case the scan must still
    catch: a mean of the price is still a price."""
    spec = StrategySpec(
        entry_logic=[
            Node(id="m", operator="rolling_mean", inputs={"series": "price.close"}, params={"window": 20}),
            Node(id="e1", operator="threshold", inputs={"series": "m"},
                 params={"upper": 500.0, "lower": -500.0}),
        ]
    )
    violations = spec_absolute_level_scan(spec)
    assert violations


# -- P0d: data provenance ------------------------------------------------------------


@pytest.fixture
def nse_market():
    return ProfileLoader().load_market("nse_equity")


def test_rejects_an_unadjusted_snapshot(nse_market):
    snapshot = {
        "validation_status": "valid",
        "in_vault": 0,
        "adjusted": 0,
        "adjustment_method": "none",
        "point_in_time_membership": 1,
    }
    checks = snapshot_provenance_checks(snapshot, nse_market)
    failing = [c for c in checks if c.result == "fail"]
    assert any(c.test_name == "corporate_actions_adjusted" for c in failing)


def test_rejects_a_non_point_in_time_universe_when_market_declares_an_index(nse_market):
    snapshot = {
        "validation_status": "valid",
        "in_vault": 0,
        "adjusted": 1,
        "adjustment_method": "back_ratio_price",
        "point_in_time_membership": 0,
    }
    checks = snapshot_provenance_checks(snapshot, nse_market)
    failing = [c for c in checks if c.result == "fail"]
    assert any(c.test_name == "point_in_time_universe" for c in failing)


def test_rejects_vault_data():
    snapshot = {
        "validation_status": "valid",
        "in_vault": 1,
        "adjusted": 1,
        "adjustment_method": "back_ratio_price",
        "point_in_time_membership": 1,
    }
    checks = snapshot_provenance_checks(snapshot, ProfileLoader().load_market("nse_equity"))
    failing = [c for c in checks if c.result == "fail"]
    assert any(c.test_name == "snapshot_outside_vault" for c in failing)


def test_accepts_a_clean_fully_provenanced_snapshot(nse_market):
    snapshot = {
        "validation_status": "valid",
        "in_vault": 0,
        "adjusted": 1,
        "adjustment_method": "back_ratio_price",
        "point_in_time_membership": 1,
    }
    checks = snapshot_provenance_checks(snapshot, nse_market)
    assert all(c.result == "pass" for c in checks)

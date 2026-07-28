"""Known-answer tests (Implementation_Plan §5.2): cases whose correct answer
is known in advance, used to validate the validator itself before any of its
verdicts are trusted."""
import pandas as pd
import pytest

from nanoaqrl._lib.backtest import empirical_leakage_scan, static_lookahead_scan
from nanoaqrl._lib.cost_models import NSE_CASH_EQUITY
from nanoaqrl._lib.synthetic_data import synthetic_path_ohlcv
from nanoaqrl._lib.walk_forward import run_best_of_three
from nanoaqrl.evaluate import _bar_check
from nanoaqrl.strategy import PARAMS as STRATEGY_PARAMS
from nanoaqrl.strategy import generate_signals as strategy_generate_signals

LOOKAHEAD_SOURCE = """
import pandas as pd

def generate_signals(df, params):
    future = df['close'].shift(-1)
    return (future > df['close']).astype(float)
"""

LEAKY_VECTORIZED_SOURCE = """
import pandas as pd

def generate_signals(df, params):
    mean = df['close'].mean()
    std = df['close'].std()
    z = (df['close'] - mean) / std
    return (z > 0).astype(float)
"""

BACKFILL_SOURCE = """
import pandas as pd

def generate_signals(df, params):
    x = df['close'].pct_change().fillna(method='bfill')
    return (x > 0).astype(float)
"""


def test_p0_static_catches_shift_negative():
    violations = static_lookahead_scan(LOOKAHEAD_SOURCE)
    assert violations, "shift(-1) must be caught"
    assert any("shift(-n)" in v for v in violations)


def test_p0_static_catches_whole_series_vectorised_leak():
    """Vectorisation is the top source of look-ahead (TRD §9.4) — this case
    is mandatory, not optional."""
    violations = static_lookahead_scan(LEAKY_VECTORIZED_SOURCE)
    assert violations
    assert any("whole-series" in v for v in violations)


def test_p0_static_catches_backfill():
    violations = static_lookahead_scan(BACKFILL_SOURCE)
    assert violations
    assert any("forbidden" in v for v in violations)


def test_p0_static_passes_the_legitimate_example_strategy():
    import inspect

    from nanoaqrl import strategy

    source = inspect.getsource(strategy)
    assert static_lookahead_scan(source) == []


def test_p0_empirical_catches_a_leak_the_static_scan_misses():
    """A strategy referencing `.iloc[-1]` of whatever slice it's given leaks
    the future into earlier signal values without tripping any forbidden
    AST pattern — only the truncation-invariance check catches it."""

    def leaky_last_value_signal(df, params):
        last_close = df["close"].iloc[-1]
        return (df["close"] > last_close).astype(float)

    df = synthetic_path_ohlcv(252 * 3, seed=5)
    assert static_lookahead_scan(
        "def generate_signals(df, params):\n    last_close = df['close'].iloc[-1]\n    return (df['close'] > last_close).astype(float)\n"
    ) == []
    violations = empirical_leakage_scan(df, leaky_last_value_signal, {})
    assert violations, "truncation-invariance check must catch the .iloc[-1] leak"


def test_p0_empirical_passes_the_legitimate_example_strategy():
    df = synthetic_path_ohlcv(252 * 3, seed=6)
    assert empirical_leakage_scan(df, strategy_generate_signals, STRATEGY_PARAMS) == []


@pytest.mark.parametrize("seed", [10, 11, 12])
def test_pure_noise_does_not_clear_the_bar(seed):
    """A strategy run against a synthetic path with zero drift and no
    structure (no alpha by construction) must not pass the bar."""
    df = synthetic_path_ohlcv(252 * 8, seed=seed)
    result = run_best_of_three(
        df, strategy_generate_signals, STRATEGY_PARAMS, NSE_CASH_EQUITY,
        holding_period_days=10, n_trials_base=1, param_grid=None,
    )
    win = result.winning_window
    from nanoaqrl._lib.backtest import max_drawdown_from_returns

    max_dd = max_drawdown_from_returns(win.concatenated_returns)
    passed, failed_on = _bar_check(win.score.honest_score, win.total_trades, max_dd, win.concatenated_returns)
    assert not passed, f"pure noise cleared the bar (score={win.score.honest_score})"

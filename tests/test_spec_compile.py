"""Compilation — a spec that actually runs, and runs the same as hand-written code.

This is where Stage 2 stops being a format and becomes usable. The headline test
is `test_a_compiled_spec_reproduces_the_handwritten_strategy`: the same idea,
written once by hand in `aqrl/research/strategy.py` and once as an operator DAG, must
produce **identical signals bar for bar**. If it does, the operator library is a
faithful re-expression of what the loop already does rather than a parallel
system with its own quiet differences.

The compiled output is also put through the research loop's own P0 scanners, because a
strategy assembled from vetted parts still has to satisfy the same gate as one
an agent wrote by hand — vetted inputs are not a substitute for the check.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import aqrl.research.strategy as handwritten
from aqrl.operators import Node, SpecError, StrategySpec, compile_spec, spec_warmup
from aqrl.research.backtest import empirical_leakage_scan, run_backtest
from aqrl.research.cost_models import get_cost_model


@pytest.fixture
def frame() -> pd.DataFrame:
    rng = np.random.default_rng(11)
    n = 800
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0004, 0.011, n))
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.006,
            "low": close * 0.994,
            "close": close,
            "volume": np.abs(rng.normal(1e4, 1.5e3, n)),
        },
        index=pd.date_range("2018-01-01", periods=n, freq="B"),
    )


def dual_ma_spec(fast: int = 20, slow: int = 100, gap: float = 0.002) -> StrategySpec:
    """The operator-DAG form of `aqrl/research/strategy.py`."""
    return StrategySpec(
        entry_logic=[
            Node(id="fast", operator="rolling_mean", params={"window": fast},
                 inputs={"series": "price.close"}),
            Node(id="slow", operator="rolling_mean", params={"window": slow},
                 inputs={"series": "price.close"}),
            Node(id="cross", operator="crossover", params={"min_gap_pct": gap},
                 inputs={"fast": "fast", "slow": "slow"}),
        ],
        hypothesis="Trend persists at a 20/100-bar horizon.",
    )


# -- the equivalence that matters -------------------------------------------------


def test_a_compiled_spec_reproduces_the_handwritten_strategy(frame):
    """Identical signals, bar for bar, against `aqrl/research/strategy.py`."""
    compiled = compile_spec(dual_ma_spec()).signals(frame)
    reference = handwritten.generate_signals(frame, handwritten.PARAMS)

    assert np.array_equal(compiled.to_numpy(), reference.to_numpy()), (
        "the operator DAG and the hand-written strategy disagree; the library is not a "
        "faithful re-expression of what the loop already does"
    )


def test_the_compiled_signal_runs_through_the_existing_backtest(frame):
    """Stage 2's standalone value: usable by hand, with no Stage 3 and no A2."""
    result = run_backtest(
        frame,
        compile_spec(dual_ma_spec()).to_signal_fn(),
        params={},
        cost_model=get_cost_model("nse_equity", "cash_equity"),
    )
    assert result.n_trades > 0
    assert np.isfinite(result.returns).all()
    assert 0.0 <= result.max_drawdown <= 1.0


def test_the_compiled_function_passes_the_p0_leakage_scan(frame):
    """Vetted inputs are not a substitute for the gate (TRD §10.1)."""
    violations = empirical_leakage_scan(
        frame, compile_spec(dual_ma_spec()).to_signal_fn(), params={}
    )
    assert violations == []


# -- semantics --------------------------------------------------------------------


def test_signals_stay_within_the_declared_range(frame):
    spec = StrategySpec(
        entry_logic=[
            Node(id="mom", operator="momentum", params={"period": 20},
                 inputs={"series": "price.close"})
        ]
    )
    values = compile_spec(spec).signals(frame).to_numpy()
    assert values.min() >= -1.0 and values.max() <= 1.0


def test_warmup_bars_are_flat_not_nan(frame):
    """NaN would poison the return series; flat is the honest reading."""
    values = compile_spec(dual_ma_spec()).signals(frame).to_numpy()
    assert np.isfinite(values).all()
    assert (values[: spec_warmup(dual_ma_spec())] == 0.0).all()


def test_a_filter_gates_without_introducing_direction(frame):
    """A filter can only ever remove exposure, never create it."""
    unfiltered = compile_spec(dual_ma_spec()).signals(frame).to_numpy()

    filtered_spec = dual_ma_spec()
    filtered = StrategySpec(
        entry_logic=filtered_spec.entry_logic,
        filter_logic=[
            Node(id="vol", operator="vol_expansion",
                 params={"period": 14, "lookback": 20, "multiple": 1.0},
                 inputs={"high": "price.high", "low": "price.low", "close": "price.close"})
        ],
    )
    gated = compile_spec(filtered).signals(frame).to_numpy()

    # Every gated bar is either the unfiltered signal or flat — never the opposite.
    assert np.all((gated == unfiltered) | (gated == 0.0))
    assert np.abs(gated).sum() < np.abs(unfiltered).sum(), "the filter removed nothing"


def test_an_exit_flattens_rather_than_reversing(frame):
    spec = StrategySpec(
        entry_logic=dual_ma_spec().entry_logic,
        exit_logic=[
            Node(id="stretch", operator="mean_reversion",
                 params={"window": 20, "entry_z": 1.0}, inputs={"series": "price.close"})
        ],
    )
    with_exit = compile_spec(spec).signals(frame).to_numpy()
    without = compile_spec(dual_ma_spec()).signals(frame).to_numpy()
    assert np.all((with_exit == without) | (with_exit == 0.0))


def test_risk_operators_receive_the_position_implicitly(frame):
    """A risk chain wraps whatever entry/exit/filter produced."""
    spec = StrategySpec(
        entry_logic=dual_ma_spec().entry_logic,
        risk_logic=[
            Node(id="stop", operator="atr_stop", params={"period": 14, "multiple": 2.0},
                 inputs={"close": "price.close", "high": "price.high", "low": "price.low"})
        ],
    )
    stopped = compile_spec(spec).signals(frame).to_numpy()
    plain = compile_spec(dual_ma_spec()).signals(frame).to_numpy()
    assert np.abs(stopped).sum() < np.abs(plain).sum(), "the stop never fired"
    assert np.all((stopped == plain) | (stopped == 0.0))


def test_risk_chain_order_is_preserved(frame):
    """Order matters in `risk_logic` and nowhere else, so it is not sorted."""
    def spec_with(order):
        return StrategySpec(entry_logic=dual_ma_spec().entry_logic, risk_logic=order)

    stop = Node(id="stop", operator="atr_stop", params={"period": 14, "multiple": 2.0},
                inputs={"close": "price.close", "high": "price.high", "low": "price.low"})
    timed = Node(id="timed", operator="time_stop", params={"max_bars": 10}, inputs={})

    forward = compile_spec(spec_with([stop, timed])).signals(frame).to_numpy()
    backward = compile_spec(spec_with([timed, stop])).signals(frame).to_numpy()
    assert not np.array_equal(forward, backward)


def test_a_shared_subgraph_is_evaluated_once(frame):
    """Memoisation is on the structural hash, so identical work is done once."""
    spec = StrategySpec(
        entry_logic=[
            Node(id="a", operator="rolling_mean", params={"window": 30},
                 inputs={"series": "price.close"}),
            Node(id="b", operator="rolling_mean", params={"window": 30},
                 inputs={"series": "price.close"}),
            Node(id="c", operator="crossover", inputs={"fast": "a", "slow": "b"}),
        ]
    )
    # `a` and `b` are structurally identical, so the crossover of the two is
    # always exactly zero — and computing it must not fail.
    values = compile_spec(spec).signals(frame).to_numpy()
    assert np.allclose(values, 0.0)


def test_price_returns_is_derived_for_operators_that_need_it(frame):
    spec = StrategySpec(
        entry_logic=dual_ma_spec().entry_logic,
        risk_logic=[
            Node(id="size", operator="vol_target",
                 params={"window": 60, "target_vol": 0.01, "max_leverage": 2.0},
                 inputs={"returns": "price.returns"})
        ],
    )
    values = compile_spec(spec).signals(frame).to_numpy()
    assert np.isfinite(values).all()
    assert np.abs(values).max() <= 1.0


def test_an_unavailable_price_column_is_reported(frame):
    spec = StrategySpec(
        entry_logic=[
            Node(id="m", operator="rolling_mean", params={"window": 20},
                 inputs={"series": "price.open_interest"})
        ]
    )
    with pytest.raises(SpecError, match="not available"):
        compile_spec(spec).signals(frame)


# -- parameter overrides ----------------------------------------------------------


def test_node_scoped_overrides_retune_without_rewriting_the_dag(frame):
    """How `evaluate.py` will tune per fold: `{"<node>.<param>": value}`."""
    spec = dual_ma_spec()
    compiled = compile_spec(spec)

    default = compiled.signals(frame).to_numpy()
    retuned = compiled.signals(frame, {"fast.window": 5}).to_numpy()
    assert not np.array_equal(default, retuned)

    explicit = compile_spec(dual_ma_spec(fast=5)).signals(frame).to_numpy()
    assert np.array_equal(retuned, explicit)


def test_an_override_is_still_range_checked(frame):
    from aqrl.operators import ParameterError

    with pytest.raises(ParameterError, match="must be >="):
        compile_spec(dual_ma_spec()).signals(frame, {"fast.window": 1})


# -- warm-up accounting -------------------------------------------------------------


def test_warmup_is_the_longest_chain_not_the_deepest_node():
    """A 20-bar z-score of a 50-bar mean needs 69 bars, not 49.

    Understating this is how a walk-forward fold silently trains on its own
    warm-up, so Stage 3 depends on it being the chain sum.
    """
    spec = StrategySpec(
        entry_logic=[
            Node(id="m", operator="rolling_mean", params={"window": 50},
                 inputs={"series": "price.close"}),
            Node(id="z", operator="zscore", params={"window": 20}, inputs={"series": "m"}),
            Node(id="t", operator="threshold", params={"upper": 1.0, "lower": -1.0},
                 inputs={"series": "z"}),
        ]
    )
    assert spec_warmup(spec) == 49 + 19


def test_a_risk_chain_stacks_on_top_of_the_signal_warmup():
    spec = StrategySpec(
        entry_logic=dual_ma_spec().entry_logic,
        risk_logic=[
            Node(id="stop", operator="atr_stop", params={"period": 14, "multiple": 2.0},
                 inputs={"close": "price.close", "high": "price.high", "low": "price.low"})
        ],
    )
    assert spec_warmup(spec) == 99 + 14

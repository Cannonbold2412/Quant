"""Validation of the validator — Implementation_Plan §5.2, and the M1 gate.

*"Before trusting it, `evaluate.py` must be tested against cases whose correct
answer is known in advance."* This module is that corpus, run through the full
engine funnel wherever the case is naturally expressed that way:

1. A deliberately look-ahead-biased strategy → P0 must catch it.
2. A deliberately leaky **vectorised** strategy (unlagged signal, whole-sample
   normalisation, backfilled NaNs) → P0 must catch it. **Mandatory** — TRD §9.4
   names vectorisation as the top source of look-ahead.
3. A pure-noise random strategy → must not pass P3.
4. A strategy overfit to one regime → the overfitting diagnostic must flag it.
5. A strategy with a real but small edge → survives P2, dies on realistic costs.
6. Analytic ground truth: a constructed series whose Sharpe and drawdown are
   derivable in closed form → engine within tolerance.
7. The same experiment single-threaded vs parallel → bit-identical
   `honest_score` (TRD §9.5).

**Done when:** all seven behave correctly, and identical inputs reproduce
identical outputs bit-for-bit (already 7, folded into this file rather than
a separate determinism suite, since it is one of the seven named cases).

### Note on cases 1 and 2

The engine evaluates **specs** — operator DAGs — not free-form Python. Stage 2
already makes causality a structural property of the operator library (every
operator is truncation-invariant by construction, enforced by a registry-wide
property test), so a spec assembled from vetted operators cannot express
`shift(-1)` or a whole-sample normalisation in the first place. Free-form
strategy code arrives at Stage 5 (A2); until then, these two mandatory cases are
proven against `p0.run_p0` directly — the actual orchestration function the
engine calls, not the scanners in isolation — with a hand-written signal
function standing in for what a future free-form strategy would submit.
"""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.eval.bar import AcceptanceBar
from aqrl.eval.engine import EvaluationInputs, evaluate_experiment
from aqrl.eval.p0 import run_p0
from aqrl.eval.panel import PricePanel
from aqrl.eval.backtest import max_drawdown_from_returns
from aqrl.eval.stats.honest_score import sharpe_ratio
from aqrl.operators.spec import Node, StrategySpec

from .conftest import bars, random_walk_bars

VALID_SNAPSHOT = {
    "validation_status": "valid",
    "in_vault": 0,
    "adjusted": 1,
    "adjustment_method": "back_ratio_price",
    "point_in_time_membership": 1,
}


def _trivial_spec() -> StrategySpec:
    """A minimal valid spec — used only to carry P0's structural checks
    (complexity, absolute-price-level scan) in cases 1 and 2, where the
    causality question is about the SIGNAL FUNCTION, not the spec DAG."""
    return StrategySpec(
        entry_logic=[
            Node(id="z", operator="zscore", inputs={"series": "price.close"}),
            Node(id="e1", operator="threshold", inputs={"series": "z"}, params={"upper": 1.0, "lower": -1.0}),
        ]
    )


def _crossover_spec(fast: int = 5, slow: int = 15) -> StrategySpec:
    return StrategySpec(
        entry_logic=[
            Node(id="fast", operator="ema", inputs={"series": "price.close"}, params={"span": fast}),
            Node(id="slow", operator="ema", inputs={"series": "price.close"}, params={"span": slow}),
            Node(id="e1", operator="crossover", inputs={"fast": "fast", "slow": "slow"}),
        ]
    )


def _inputs(resolved, panel, **overrides) -> EvaluationInputs:
    fields = dict(
        spec=_crossover_spec(),
        panel=panel,
        resolved=resolved,
        snapshot=VALID_SNAPSHOT,
        acceptance_bar=AcceptanceBar(min_trades=1),
        family_prior_trials=0,
        random_seed=1,
        replications=50,
    )
    fields.update(overrides)
    return EvaluationInputs(**fields)


# ===========================================================================
# Case 1 — look-ahead-biased strategy: P0 must catch it
# ===========================================================================


LOOK_AHEAD_SOURCE = """
import pandas as pd

def generate_signals(df, params):
    future = df['close'].shift(-1)
    return (future > df['close']).astype(float)
"""


def test_case_1_look_ahead_biased_strategy_is_caught_by_p0(resolved):
    """A textbook `shift(-1)` leak.

    **Only the static AST scan catches this one, and that is by design, not a
    gap in the empirical scan.** A pure `shift(-1)` leak differs from the
    causal version at exactly ONE point near each truncation boundary — every
    earlier point is completely unaffected — and `empirical_leakage_scan_arrays`
    deliberately excludes the last few points of every truncation window
    (`p0.py`'s "ignore the tail" comment) because a genuinely causal rolling
    window legitimately differs there too, once too little of it remains. The
    static scan exists precisely to catch what that necessary tolerance would
    otherwise let through, which is why `source` must be supplied here — the
    same reason nanoAQRL's own known-answer suite (`nanoaqrl/tests/test_bar_and_p0.py`)
    exercises this case through the static scanner, not the empirical one.
    """
    rng = np.random.default_rng(0)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, 400))

    def look_ahead_signal(columns: dict) -> np.ndarray:
        series = columns["close"]
        future = np.roll(series, -1)
        future[-1] = series[-1]
        return np.where(future > series, 1.0, -1.0)

    checks = run_p0(
        _trivial_spec(),
        look_ahead_signal,
        [("AAA", {"close": close})],
        VALID_SNAPSHOT,
        resolved.market,
        source=LOOK_AHEAD_SOURCE,
    )
    failing = [c for c in checks if c.blocking]
    assert failing, "a look-ahead-biased strategy must fail at least one P0 check"
    assert any(c.test_name == "static_lookahead_scan" for c in failing)


# ===========================================================================
# Case 2 — leaky VECTORISED strategy: mandatory per TRD §9.4
# ===========================================================================


def test_case_2_leaky_vectorised_strategy_is_caught_by_p0(resolved):
    """Whole-sample normalisation, backfilled NaNs — the textbook vectorised
    leak TRD §9.4 calls out by name. Mandatory, not optional."""
    rng = np.random.default_rng(1)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, 400))

    def leaky_vectorised_signal(columns: dict) -> np.ndarray:
        series = columns["close"]
        # Whole-sample normalisation: every point's z-score depends on the
        # mean and std of the ENTIRE series, including points from its own
        # future.
        mean, std = series.mean(), series.std()
        z = (series - mean) / std
        # Backfilled NaN: the leading warm-up is filled from FUTURE values.
        z = np.where(np.isnan(z), np.nan, z)
        z[:5] = np.nan
        filled = z.copy()
        for i in range(4, -1, -1):
            filled[i] = filled[i + 1]  # pulls a later value backwards
        return np.where(filled > 0.0, 1.0, -1.0)

    checks = run_p0(
        _trivial_spec(),
        leaky_vectorised_signal,
        [("AAA", {"close": close})],
        VALID_SNAPSHOT,
        resolved.market,
    )
    failing = [c for c in checks if c.blocking]
    assert failing, "a whole-sample-normalised, backfilled signal must fail P0"
    assert any(c.test_name == "truncation_invariance" for c in failing)


def test_case_2_a_causal_rolling_signal_passes_the_same_check(resolved):
    """The negative control: a scan that cannot fail manufactures confidence
    rather than providing it (Stage 2's own precedent)."""
    rng = np.random.default_rng(2)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, 400))

    def causal_signal(columns: dict) -> np.ndarray:
        series = columns["close"]
        out = np.full(series.size, np.nan)
        window = 20
        for i in range(window, series.size):
            trailing = series[i - window : i]
            out[i] = 1.0 if series[i] > trailing.mean() else -1.0
        return np.nan_to_num(out, nan=0.0)

    checks = run_p0(
        _trivial_spec(), causal_signal, [("AAA", {"close": close})], VALID_SNAPSHOT, resolved.market
    )
    assert not [c for c in checks if c.blocking]


# ===========================================================================
# Case 3 — pure noise must not pass P3
# ===========================================================================


@pytest.mark.parametrize("seed", [10, 11, 12])
def test_case_3_pure_noise_does_not_pass_p3(resolved, seed):
    """Zero drift, no structure: no alpha by construction. Must not clear
    the funnel, whichever phase it dies at."""
    frame = random_walk_bars(252 * 8, instruments=2, seed=seed, drift=0.0, volatility=0.012)
    panel = PricePanel.from_frame(frame)
    report = evaluate_experiment(_inputs(resolved, panel, random_seed=seed))

    assert report.outcome != "passed", (
        f"pure noise (seed={seed}) cleared the funnel: phase={report.phase_reached}"
    )


# ===========================================================================
# Case 4 — overfit to one regime: the diagnostic must flag it
# ===========================================================================


def _regime_split_frame(seed: int, n_half: int = 900):
    """First half a strong deterministic uptrend; second half pure noise
    around the level the trend reached. A parameter grid tuned per fold can
    only latch onto the trending half — folds whose training window falls in
    the noise half select a configuration on nothing.
    """
    rng = np.random.default_rng(seed)
    trend = 100.0 * np.cumprod(1.0 + 0.002 + rng.normal(0.0, 0.004, n_half))
    noise = trend[-1] * np.cumprod(1.0 + rng.normal(0.0, 0.01, n_half))
    closes = list(trend) + list(noise)
    return bars({"AAA": closes, "BBB": [c * 0.6 for c in closes]})


@pytest.mark.parametrize("seed", [1, 4, 5])
def test_case_4_regime_dependent_overfitting_collapses_wf_efficiency(resolved, seed):
    """TRD §8.6: a large tuning grid does not inflate `N_trials` — it inflates
    the chance each fold overfits internally, which shows up as walk-forward
    efficiency collapsing. **That is the diagnostic to watch.** A strict PBO
    threshold is not asserted here: with only a handful of folds contributing
    to the CSCV matrix, PBO is measurably noisy across seeds (observed 0.05 to
    0.61 in prototyping) — reported, but not gated on, for exactly that reason.
    """
    panel = PricePanel.from_frame(_regime_split_frame(seed))
    loose_bar = AcceptanceBar(min_trades=1, min_score=-100.0, max_drawdown=0.99)
    report = evaluate_experiment(
        _inputs(
            resolved, panel, acceptance_bar=loose_bar, random_seed=seed, replications=50,
            param_grid={"fast.span": [5, 10, 20, 30], "slow.span": [40, 50, 75, 100]},
        )
    )

    assert report.best_of_three is not None, (
        f"expected a computed score to inspect wf_efficiency on; got phase={report.phase_reached}"
    )
    assert report.best_of_three.winning_window.wf_efficiency < 0.8


# ===========================================================================
# Case 5 — a real but small edge: survives P2, dies on realistic costs
# ===========================================================================


def test_case_5_small_edge_survives_light_costs_dies_at_2x(resolved):
    """A small, genuine, oscillating edge sized so its round-trip P&L clears
    1x costs (~27.7bps on this profile) but not the 2x default (TRD §7.2:
    cost stress is the default condition, not a separate later test)."""
    t = np.arange(1200)
    closes = list(100.0 * np.exp(0.006 * np.sin(2 * np.pi * t / 40)))
    frame = bars({"AAA": closes, "BBB": [c * 0.8 for c in closes]})
    panel = PricePanel.from_frame(frame)

    light = evaluate_experiment(_inputs(resolved, panel, cost_multiplier=1.0))
    assert light.outcome != "failed" or light.phase_reached not in ("P2",), (
        f"the edge should survive light costs; got {light.phase_reached}/{light.failure_reason}"
    )

    realistic = evaluate_experiment(_inputs(resolved, panel, cost_multiplier=2.0))
    assert realistic.phase_reached == "P2"
    assert realistic.failure_reason == "costs_exceed_edge"


# ===========================================================================
# Case 6 — analytic ground truth
# ===========================================================================


def test_case_6_sharpe_matches_a_closed_form_derivation():
    """Deterministic alternating returns: mean, std and Sharpe are all
    derivable by hand, independent of the implementation under test."""
    returns = np.array([0.02, -0.01] * 500)
    mean = 0.005
    # ddof=1 sample std of an exactly-alternating series of even length has a
    # closed form: std = |diff|/2 * sqrt(n/(n-1)).
    expected_std = 0.015 * np.sqrt(1000 / 999)
    expected_sharpe = mean / expected_std * np.sqrt(252)

    assert sharpe_ratio(returns, periods_per_year=252) == pytest.approx(expected_sharpe, rel=1e-6)


def test_case_6_drawdown_matches_a_closed_form_derivation():
    """Two-bar cycles of +2%/-1%: each cycle sets a new peak on the up-bar and
    gives back exactly 1% of it on the down-bar, so max drawdown is exactly
    1% — derivable without running the strategy at all."""
    returns = np.array([0.02, -0.01] * 500)
    assert max_drawdown_from_returns(returns) == pytest.approx(0.01, abs=1e-9)


def test_case_6_round_trip_cost_matches_the_profiles_own_arithmetic(resolved):
    """The NSE cash-equity profile's round-trip cost is entry_bps + exit_bps
    by definition (`CostModel.round_trip_bps`) — a closed-form check that the
    engine's cost application (`costs.transaction_costs`) reproduces exactly
    what the profile declares, not some rescaled or halved version of it."""
    from aqrl.eval.costs import transaction_costs

    cost_model = resolved.cost_model
    weights = np.array([[0.0], [1.0], [0.0]])
    costs = transaction_costs(weights, cost_model, multiplier=1.0)
    assert float(costs.sum()) == pytest.approx(cost_model.round_trip_bps() * 1e-4, rel=1e-9)


# ===========================================================================
# Case 7 — single-threaded vs parallel: bit-identical honest_score (TRD §9.5)
# ===========================================================================


def test_case_7_single_threaded_and_parallel_are_bit_identical(resolved):
    frame = random_walk_bars(252 * 6, instruments=3, seed=5, drift=0.0004)
    panel = PricePanel.from_frame(frame)

    single = evaluate_experiment(_inputs(resolved, panel, random_seed=7, workers=1))
    parallel = evaluate_experiment(_inputs(resolved, panel, random_seed=7, workers=4))

    assert single.phase_reached == parallel.phase_reached
    assert single.outcome == parallel.outcome
    if single.best_of_three is not None:
        assert single.best_of_three.score.honest_score == parallel.best_of_three.score.honest_score
        assert np.array_equal(
            single.best_of_three.winning_window.concatenated_returns,
            parallel.best_of_three.winning_window.concatenated_returns,
        )


def test_case_7_a_rerun_of_the_same_experiment_is_bit_identical(resolved):
    frame = random_walk_bars(252 * 6, instruments=2, seed=8, drift=0.0005)
    panel = PricePanel.from_frame(frame)

    first = evaluate_experiment(_inputs(resolved, panel, random_seed=13))
    second = evaluate_experiment(_inputs(resolved, panel, random_seed=13))

    assert first.phase_reached == second.phase_reached
    if first.best_of_three is not None:
        assert first.best_of_three.score.honest_score == second.best_of_three.score.honest_score

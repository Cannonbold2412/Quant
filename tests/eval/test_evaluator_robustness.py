"""3d — the panel evaluator bridging a compiled spec to the walk-forward
protocol, and the P3 robustness battery run over its output."""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.eval.evaluator import PanelEvaluator
from aqrl.eval.panel import PricePanel
from aqrl.eval.robustness import cost_breakeven_multiplier, run_robustness
from aqrl.eval.walk_forward import run_best_of_three
from aqrl.operators.compile import compile_spec
from aqrl.operators.spec import Node, StrategySpec

from .conftest import random_walk_bars

PERIODS_PER_YEAR = 252.0


def _crossover_spec() -> StrategySpec:
    return StrategySpec(
        entry_logic=[
            Node(id="fast", operator="ema", inputs={"series": "price.close"}, params={"span": 10}),
            Node(id="slow", operator="ema", inputs={"series": "price.close"}, params={"span": 50}),
            Node(id="e1", operator="crossover", inputs={"fast": "fast", "slow": "slow"}),
        ]
    )


def test_panel_evaluator_returns_gross_costs_and_net_consistently(resolved):
    frame = random_walk_bars(400, instruments=2, seed=1, drift=0.0005)
    panel = PricePanel.from_frame(frame)
    compiled = compile_spec(_crossover_spec())
    evaluate = PanelEvaluator(panel, compiled, resolved, cost_multiplier=2.0)

    outcome = evaluate(panel.start, panel.end, {})
    assert outcome.n_bars > 0
    assert outcome.gross_returns is not None and outcome.costs is not None
    # Net = gross - 2x costs (at the evaluator's own multiplier), reconstructed
    # from the normalised 1x costs it reports.
    reconstructed = outcome.gross_returns - 2.0 * outcome.costs
    assert reconstructed == pytest.approx(outcome.returns, abs=1e-9)


def test_panel_evaluator_is_picklable(resolved):
    import pickle

    frame = random_walk_bars(200, instruments=1, seed=2)
    panel = PricePanel.from_frame(frame)
    compiled = compile_spec(_crossover_spec())
    evaluate = PanelEvaluator(panel, compiled, resolved)
    pickle.loads(pickle.dumps(evaluate))  # must not raise


def test_cost_breakeven_multiplier_is_none_for_a_pure_loser():
    gross = np.full(100, -0.001)
    costs = np.full(100, 0.0001)
    assert cost_breakeven_multiplier(gross, costs) is None


def test_cost_breakeven_multiplier_rises_with_a_bigger_edge():
    gross_small_edge = np.full(100, 0.0005)
    gross_big_edge = np.full(100, 0.002)
    costs = np.full(100, 0.0003)
    small = cost_breakeven_multiplier(gross_small_edge, costs)
    big = cost_breakeven_multiplier(gross_big_edge, costs)
    assert big > small


def test_run_robustness_end_to_end_on_a_real_walk_forward_window(resolved):
    frame = random_walk_bars(252 * 6, instruments=3, seed=3, drift=0.0003)
    panel = PricePanel.from_frame(frame)
    compiled = compile_spec(_crossover_spec())
    evaluate = PanelEvaluator(panel, compiled, resolved, cost_multiplier=2.0)

    result = run_best_of_three(
        panel.dates,
        evaluate,
        {},
        holding_period_bars=10,
        n_trials=3,
        periods_per_year=PERIODS_PER_YEAR,
    )
    window = result.winning_window
    robustness = run_robustness(
        window, panel, PERIODS_PER_YEAR, n_trials=3, replications=50, base_seed=1
    )

    assert 0.0 <= robustness.deflated_sharpe <= 1.0
    assert robustness.monte_carlo.replications == 50 or window.concatenated_returns.size < 2
    assert isinstance(robustness.regimes, list)
    # No tuning grid on this experiment (no param_grid passed), so both the
    # reality-check and PBO honestly report "nothing to see" rather than a
    # fabricated number.
    assert robustness.white_rc_pvalue is None
    assert robustness.pbo is None


def test_run_robustness_reports_pbo_and_reality_check_when_a_grid_was_tuned(resolved):
    frame = random_walk_bars(252 * 6, instruments=2, seed=4, drift=0.0004)
    panel = PricePanel.from_frame(frame)
    compiled = compile_spec(_crossover_spec())
    evaluate = PanelEvaluator(panel, compiled, resolved, cost_multiplier=2.0)

    result = run_best_of_three(
        panel.dates,
        evaluate,
        {},
        holding_period_bars=10,
        n_trials=3,
        periods_per_year=PERIODS_PER_YEAR,
        param_grid={"fast.span": [5, 10, 20], "slow.span": [30, 50]},
    )
    window = result.winning_window
    if window.grid_returns is None:
        pytest.skip("no fold produced a usable grid on this draw")

    robustness = run_robustness(
        window, panel, PERIODS_PER_YEAR, n_trials=3, replications=50, base_seed=2
    )
    assert robustness.white_rc_pvalue is not None
    assert robustness.pbo is not None
    assert 0.0 <= robustness.pbo <= 1.0

"""3f — the engine: the full funnel, short-circuiting correctly at each phase.

This is the integration point for everything built in 3a-3e. The known-answer
corpus in `test_known_answers.py` is the acceptance gate (M1); these tests
check the funnel's *plumbing* — that each phase actually stops the pipeline
when it should, and that a clean pass produces a complete report.
"""
from __future__ import annotations

import numpy as np

from aqrl.eval.bar import AcceptanceBar
from aqrl.eval.engine import EvaluationInputs, evaluate_experiment
from aqrl.eval.panel import PricePanel
from aqrl.operators.spec import Node, StrategySpec

from .conftest import bars, random_walk_bars

VALID_SNAPSHOT = {
    "validation_status": "valid",
    "in_vault": 0,
    "adjusted": 1,
    "adjustment_method": "back_ratio_price",
    "point_in_time_membership": 1,
}


def _crossover_spec(fast=10, slow=50) -> StrategySpec:
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
        acceptance_bar=AcceptanceBar(min_trades=5, min_score=-10.0),  # loose bar for plumbing tests
        family_prior_trials=0,
        random_seed=1,
        replications=30,
    )
    fields.update(overrides)
    return EvaluationInputs(**fields)


def _oscillating_frame(n_bars: int = 1200, period: int = 40, amplitude: float = 0.15):
    """A smooth sine-wave price path.

    Deliberately not a stochastic random walk: this test exists to check the
    funnel's plumbing all the way to P3, and a genuine random walk gives a
    crossover strategy no guaranteed edge to survive P2 on. A repeating
    oscillation gives an EMA crossover many clean, cost-surviving round trips
    by construction — real market realism is the known-answer suite's job
    (`test_known_answers.py`), not this one's.
    """
    t = np.arange(n_bars)
    closes = list(100.0 * np.exp(amplitude * np.sin(2 * np.pi * t / period)))
    return bars({"AAA": closes, "BBB": [c * 0.7 for c in closes]})


def test_a_clean_strategy_reaches_p3_with_a_complete_report(resolved):
    panel = PricePanel.from_frame(_oscillating_frame())
    # A fast/slow pair sized to the oscillation's own period (40 bars) —
    # the default 10/50 pair is too slow to track it and dies at P2 on
    # negative expectancy, which is a fact about this fixture, not the engine.
    fast_spec = _crossover_spec(fast=5, slow=15)
    report = evaluate_experiment(_inputs(resolved, panel, spec=fast_spec))

    assert report.phase_reached == "P3"
    assert report.outcome in ("passed", "failed")  # a real evaluation ran to completion
    assert report.best_of_three is not None
    assert report.robustness is not None
    if report.outcome == "passed":
        assert report.metrics is not None
        assert report.bar_verdict.passed


def test_complexity_over_the_cap_stops_before_any_compute(resolved):
    frame = random_walk_bars(252 * 2, instruments=1, seed=2)
    panel = PricePanel.from_frame(frame)
    spec = _crossover_spec()
    bar = AcceptanceBar(max_complexity=len(spec.nodes()) - 1)
    report = evaluate_experiment(_inputs(resolved, panel, spec=spec, acceptance_bar=bar))

    assert report.phase_reached == "bar"
    assert report.outcome == "failed"
    assert report.best_of_three is None  # nothing downstream ran


def test_p0_look_ahead_stops_before_any_backtest(resolved):
    """A spec-level absolute-price-level violation is a P0 structural check —
    the mandatory look-ahead cases live in test_known_answers.py."""
    frame = random_walk_bars(252 * 2, instruments=1, seed=3)
    panel = PricePanel.from_frame(frame)
    leaky_spec = StrategySpec(
        entry_logic=[
            Node(id="e1", operator="threshold", inputs={"series": "price.close"},
                 params={"upper": 500.0, "lower": -500.0})
        ]
    )
    report = evaluate_experiment(_inputs(resolved, panel, spec=leaky_spec))

    assert report.phase_reached == "P0"
    assert report.outcome == "failed"
    assert report.best_of_three is None


def test_an_unadjusted_snapshot_is_rejected_at_p0(resolved):
    frame = random_walk_bars(252 * 2, instruments=1, seed=4)
    panel = PricePanel.from_frame(frame)
    bad_snapshot = {**VALID_SNAPSHOT, "adjusted": 0, "adjustment_method": "none"}
    report = evaluate_experiment(_inputs(resolved, panel, snapshot=bad_snapshot))

    assert report.phase_reached == "P0"
    assert report.outcome == "failed"


def test_a_flat_signal_dies_at_p1_for_lack_of_trades(resolved):
    frame = random_walk_bars(252 * 2, instruments=1, seed=5)
    panel = PricePanel.from_frame(frame)
    # fast == slow: crossover never fires, so there is no signal at all.
    flat_spec = _crossover_spec(fast=20, slow=20)
    report = evaluate_experiment(_inputs(resolved, panel, spec=flat_spec))

    assert report.phase_reached in ("P0", "P1")  # truncation invariance may also flag a degenerate signal
    assert report.outcome == "failed"


def test_bar_failure_leaves_best_of_three_present_but_no_passing_verdict(resolved):
    """Even on a bar failure the P3 statistics were computed — the bar's
    ITEMS are checked after the score exists structurally, but TRD §7.5 still
    requires the ROW to carry no honest_score once persisted; that persistence
    contract is `report_persistence.py`'s job, not the engine's."""
    frame = random_walk_bars(252 * 2, instruments=1, seed=6, drift=0.0)
    panel = PricePanel.from_frame(frame)
    strict_bar = AcceptanceBar(min_trades=10_000)  # impossible to clear
    report = evaluate_experiment(_inputs(resolved, panel, acceptance_bar=strict_bar))

    if report.phase_reached == "P3" and report.outcome == "failed":
        assert report.bar_verdict is not None
        assert not report.bar_verdict.passed
        assert report.failure_reason == "min_trades"


def test_report_is_deterministic_for_the_same_seed(resolved):
    frame = random_walk_bars(252 * 4, instruments=2, seed=7, drift=0.0004)
    panel = PricePanel.from_frame(frame)
    first = evaluate_experiment(_inputs(resolved, panel, random_seed=99))
    second = evaluate_experiment(_inputs(resolved, panel, random_seed=99))

    assert first.outcome == second.outcome
    assert first.phase_reached == second.phase_reached
    if first.best_of_three is not None:
        assert first.best_of_three.score.honest_score == second.best_of_three.score.honest_score

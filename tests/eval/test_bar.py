"""The hard bar — each item fails in isolation, and a bar failure computes
no score (TRD §7.5)."""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.eval.bar import (
    AcceptanceBar,
    breadth_of,
    check_complexity,
    check_min_score,
    check_outcome,
    complexity_of,
)
from aqrl.operators.spec import Node, StrategySpec


def _spec(n_nodes: int = 1) -> StrategySpec:
    nodes = [
        Node(id=f"m{i}", operator="rolling_mean", inputs={"series": "price.close"}, params={"window": 10 + i})
        for i in range(n_nodes)
    ]
    return StrategySpec(entry_logic=[
        Node(id="e1", operator="threshold", inputs={"series": nodes[0].id if nodes else "price.close"},
             params={"upper": 1.0, "lower": -1.0})
    ] if not nodes else nodes[:1] + [
        Node(id="e1", operator="threshold", inputs={"series": nodes[0].id}, params={"upper": 1.0, "lower": -1.0})
    ])


# -- complexity --------------------------------------------------------------------


def test_complexity_counts_reachable_nodes():
    spec = _spec(n_nodes=1)
    assert complexity_of(spec) == len(spec.nodes())


def test_complexity_check_fails_over_the_cap():
    spec = _spec(n_nodes=1)
    bar = AcceptanceBar(max_complexity=complexity_of(spec) - 1)
    result = check_complexity(spec, bar)
    assert result.result == "fail"


def test_complexity_check_warns_rather_than_gates_when_uncapped():
    spec = _spec(n_nodes=1)
    bar = AcceptanceBar(max_complexity=None)
    result = check_complexity(spec, bar)
    assert result.result == "warn"
    assert result.gating is False


# -- breadth -----------------------------------------------------------------------


def test_breadth_is_the_fraction_of_traded_instruments_that_won():
    pnl = np.array([1.0, -1.0, 2.0, 0.5])
    trades = np.array([5, 5, 5, 0])  # last instrument never traded
    assert breadth_of(pnl, trades) == pytest.approx(2 / 3)  # 2 of 3 TRADED instruments won


def test_breadth_is_zero_when_nothing_traded():
    assert breadth_of(np.array([1.0, -1.0]), np.array([0, 0])) == 0.0


# -- data-dependent outcome ----------------------------------------------------------


def test_min_trades_fails_in_isolation():
    bar = AcceptanceBar(min_trades=100)
    verdict = check_outcome(bar, n_trades=50, max_drawdown=0.05, oos_return_total=0.1, breadth=1.0)
    assert not verdict.passed
    assert verdict.failed_on == "min_trades"


def test_max_drawdown_fails_in_isolation():
    bar = AcceptanceBar(min_trades=10, max_drawdown=0.15)
    verdict = check_outcome(bar, n_trades=200, max_drawdown=0.30, oos_return_total=0.1, breadth=1.0)
    assert not verdict.passed
    assert verdict.failed_on == "max_drawdown"


def test_cost_stress_fails_in_isolation():
    bar = AcceptanceBar(min_trades=10)
    verdict = check_outcome(bar, n_trades=200, max_drawdown=0.05, oos_return_total=-0.1, breadth=1.0)
    assert not verdict.passed
    assert verdict.failed_on == "cost_stress"


def test_breadth_fails_in_isolation_when_locked():
    bar = AcceptanceBar(min_trades=10, min_breadth=0.5)
    verdict = check_outcome(bar, n_trades=200, max_drawdown=0.05, oos_return_total=0.1, breadth=0.2)
    assert not verdict.passed
    assert verdict.failed_on == "breadth"


def test_a_clean_pass_flags_nothing():
    bar = AcceptanceBar(min_trades=100, max_drawdown=0.15)
    verdict = check_outcome(bar, n_trades=200, max_drawdown=0.05, oos_return_total=0.1, breadth=0.6)
    assert verdict.passed
    assert verdict.failed_on is None


def test_market_drawdown_override_applies():
    from aqrl.profiles import ProfileLoader

    crypto_like = ProfileLoader().load_market("nse_equity").model_copy(update={"max_drawdown_override": 0.20})
    bar = AcceptanceBar(max_drawdown=0.15).for_market(crypto_like)
    assert bar.max_drawdown == 0.20


# -- score itself is the last item ---------------------------------------------------


def test_min_score_check_is_the_last_gate():
    bar = AcceptanceBar(min_score=0.5)
    assert check_min_score(bar, honest_score=0.6).result == "pass"
    assert check_min_score(bar, honest_score=0.4).result == "fail"

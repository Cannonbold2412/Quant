"""3e — market-specific gates: extra phases appended, never a forked engine.

Each gate is tested against a synthetic profile variant rather than a real
crypto/commodity/forex YAML, since none ships yet (`Implementation_Plan.md`
§21) — the point of TRD §6.5 is that the *engine* code needs no market-specific
branch, only a profile that claims the relevant asset class or hazard.
"""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.eval.backtest import run_backtest
from aqrl.eval.gates import run_market_gates
from aqrl.eval.gates.context import GateContext
from aqrl.eval.gates import commodities, crypto, equities, forex
from aqrl.eval.panel import PricePanel
from aqrl.profiles import ProfileLoader

from .conftest import bars, random_walk_bars


@pytest.fixture
def nse_resolved():
    return ProfileLoader().resolve("nse_equity", "daily", "cash_equity")


def _context(resolved, panel, **overrides):
    result = run_backtest(panel, np.ones((panel.n_bars, panel.n_instruments)), resolved)
    return GateContext(panel=panel, result=result, resolved=resolved, **overrides)


# -- equities -----------------------------------------------------------------------


def test_equities_survivorship_passes_when_market_declares_no_hazard(nse_resolved):
    # The shipped nse_equity profile DOES declare survivorship (TRD §14.3,
    # blocked on data) — so a market with no such hazard has to be synthesised
    # to exercise the "not applicable" branch.
    market = nse_resolved.market.model_copy(
        update={"hazards": nse_resolved.market.hazards.model_copy(update={"survivorship": False})}
    )
    resolved = nse_resolved.model_copy(update={"market": market})
    panel = PricePanel.from_frame(bars([100.0] * 5))
    context = _context(resolved, panel)
    checks = equities.run(context)
    survivorship = next(c for c in checks if c.test_name == "equities_survivorship")
    assert survivorship.result != "fail"


def test_equities_survivorship_fails_when_hazard_declared_and_not_handled(nse_resolved):
    market = nse_resolved.market.model_copy(
        update={"hazards": nse_resolved.market.hazards.model_copy(update={"survivorship": True})}
    )
    resolved = nse_resolved.model_copy(update={"market": market})
    panel = PricePanel.from_frame(bars([100.0] * 5))
    context = _context(resolved, panel, survivorship_handled=False)
    result = equities._survivorship(context)
    assert result.result == "fail"


def test_equities_survivorship_passes_when_hazard_declared_and_handled(nse_resolved):
    market = nse_resolved.market.model_copy(
        update={"hazards": nse_resolved.market.hazards.model_copy(update={"survivorship": True})}
    )
    resolved = nse_resolved.model_copy(update={"market": market})
    panel = PricePanel.from_frame(bars([100.0] * 5))
    context = _context(resolved, panel, survivorship_handled=True)
    assert equities._survivorship(context).result == "pass"


def test_equities_capacity_flags_thin_volume(nse_resolved):
    frame = bars([100.0] * 20, volume=1.0)  # almost no volume at all
    panel = PricePanel.from_frame(frame)
    context = _context(nse_resolved, panel, assumed_capital=10_000_000.0)
    result = equities._capacity(context)
    assert result.result == "fail"


def test_equities_capacity_passes_with_deep_volume(nse_resolved):
    frame = bars([100.0] * 20, volume=1e12)
    panel = PricePanel.from_frame(frame)
    context = _context(nse_resolved, panel, assumed_capital=10_000.0)
    result = equities._capacity(context)
    assert result.result == "pass"


def test_equities_capacity_warns_without_a_locked_cap():
    loader = ProfileLoader()
    market = loader.load_market("nse_equity")
    market = market.model_copy(update={"reference": market.reference.model_copy(update={"adv_participation_cap_pct": None})})
    resolved = loader.resolve("nse_equity", "daily", "cash_equity").model_copy(update={"market": market})
    panel = PricePanel.from_frame(bars([100.0] * 5))
    context = _context(resolved, panel)
    result = equities._capacity(context)
    assert result.result == "warn"
    assert result.gating is False


# -- crypto -------------------------------------------------------------------------


@pytest.fixture
def crypto_resolved(nse_resolved):
    """A synthetic perpetual-flavoured profile: cash_equity's cost model with a
    funding rate bolted on, since no real crypto profile ships yet."""
    cost_model = nse_resolved.cost_model.model_copy(
        update={"asset_class": "perpetual", "funding_rate_bps_per_day": 3.0}
    )
    return nse_resolved.model_copy(update={"cost_model": cost_model})


def test_funding_sensitivity_is_a_warning_with_no_funding_rate(nse_resolved):
    panel = PricePanel.from_frame(bars([100.0] * 10))
    context = _context(nse_resolved, panel)
    result = crypto._funding_sensitivity(context)
    assert result.result == "warn"


def test_funding_sensitivity_measures_a_real_edge(crypto_resolved):
    frame = random_walk_bars(300, instruments=1, seed=0, drift=0.001)
    panel = PricePanel.from_frame(frame)
    context = _context(crypto_resolved, panel)
    result = crypto._funding_sensitivity(context)
    assert result.value is not None and result.value >= 0.0


def test_venue_robustness_warns_with_no_second_venue(nse_resolved):
    panel = PricePanel.from_frame(bars([100.0] * 10))
    context = _context(nse_resolved, panel)
    assert crypto._venue_robustness(context).result == "warn"


def test_venue_robustness_passes_for_highly_correlated_venues(nse_resolved):
    panel = PricePanel.from_frame(random_walk_bars(200, instruments=1, seed=1))
    context = _context(nse_resolved, panel)
    secondary = context.result.portfolio_returns.copy()
    context = GateContext(
        panel=context.panel, result=context.result, resolved=context.resolved,
        secondary_venue_returns=secondary,
    )
    result = crypto._venue_robustness(context)
    assert result.result == "pass"
    assert result.value == pytest.approx(1.0, abs=1e-6)


def test_venue_robustness_fails_for_uncorrelated_venues(nse_resolved):
    panel = PricePanel.from_frame(random_walk_bars(200, instruments=1, seed=2))
    context = _context(nse_resolved, panel)
    rng = np.random.default_rng(99)
    secondary = rng.normal(0.0, 0.01, context.result.portfolio_returns.size)
    context = GateContext(
        panel=context.panel, result=context.result, resolved=context.resolved,
        secondary_venue_returns=secondary,
    )
    result = crypto._venue_robustness(context)
    assert result.value < MIN_CORR_THRESHOLD if (MIN_CORR_THRESHOLD := 0.5) else True


# -- commodities ----------------------------------------------------------------------


def test_roll_sensitivity_warns_with_no_data(nse_resolved):
    panel = PricePanel.from_frame(bars([100.0] * 10))
    context = _context(nse_resolved, panel)
    result = commodities._roll_sensitivity(context)
    assert result.result == "warn"


def test_roll_sensitivity_passes_for_correlated_roll_methods(nse_resolved):
    panel = PricePanel.from_frame(random_walk_bars(200, instruments=1, seed=3))
    result_bt = run_backtest(panel, np.ones((panel.n_bars, 1)), nse_resolved)
    context = GateContext(
        panel=panel, result=result_bt, resolved=nse_resolved,
        alternate_roll_returns=result_bt.portfolio_returns.copy(),
    )
    assert commodities._roll_sensitivity(context).result == "pass"


def test_gate_registry_includes_commodities_when_hazard_declared(nse_resolved):
    market = nse_resolved.market.model_copy(
        update={"hazards": nse_resolved.market.hazards.model_copy(update={"contract_roll": True})}
    )
    resolved = nse_resolved.model_copy(update={"market": market})
    panel = PricePanel.from_frame(bars([100.0] * 10))
    context = _context(resolved, panel)
    checks = run_market_gates(context)
    names = {c.test_name for c in checks}
    assert "commodities_roll_sensitivity" in names


# -- forex --------------------------------------------------------------------------


def test_session_dependence_is_not_applicable_at_daily_bars(nse_resolved):
    panel = PricePanel.from_frame(bars([100.0] * 10))
    context = _context(nse_resolved, panel, is_forex=True)
    result = forex._session_dependence(context)
    assert result.result == "warn"
    assert "not applicable" in (result.detail or "")


def test_carry_decomposition_warns_with_no_carry_rate(nse_resolved):
    panel = PricePanel.from_frame(bars([100.0] * 10))
    context = _context(nse_resolved, panel, is_forex=True)
    assert forex._carry_decomposition(context).result == "warn"


def test_carry_decomposition_reports_a_fraction_when_a_rate_is_set(crypto_resolved):
    frame = random_walk_bars(200, instruments=1, seed=5, drift=0.0005)
    panel = PricePanel.from_frame(frame)
    context = _context(crypto_resolved, panel, is_forex=True)
    result = forex._carry_decomposition(context)
    assert result.gating is False
    assert result.value is not None and result.value >= 0.0


# -- registry selection ---------------------------------------------------------------


def test_registry_selects_equities_gates_for_cash_equity(nse_resolved):
    panel = PricePanel.from_frame(bars([100.0] * 10))
    context = _context(nse_resolved, panel)
    names = {c.test_name for c in run_market_gates(context)}
    assert {"equities_survivorship", "equities_capacity"} <= names
    assert not any(name.startswith("crypto_") for name in names)


def test_registry_selects_crypto_gates_for_perpetuals(crypto_resolved):
    panel = PricePanel.from_frame(bars([100.0] * 10))
    context = _context(crypto_resolved, panel)
    names = {c.test_name for c in run_market_gates(context)}
    assert {"crypto_funding_sensitivity", "crypto_venue_robustness"} <= names

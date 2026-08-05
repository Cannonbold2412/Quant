"""Stage 11's pure scoring layer (Implementation_Plan §14) — no database, no
git. Runs everywhere, including native Windows, unlike
`tests/orchestration/test_monitoring.py` which needs `vcs.py`'s `fcntl`.

`replay()` itself needs a database (strategies/experiments/snapshots) and is
exercised end to end there instead; here `ReplayResult` is built by hand from
two independently-backtested eras, exactly the shape `replay()` produces.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from aqrl import monitoring
from aqrl.config import Settings
from aqrl.eval.backtest import run_backtest
from aqrl.eval.metrics import compute_metrics
from aqrl.eval.panel import PricePanel
from aqrl.eval.tradebook import extract_trades
from aqrl.profiles import ProfileLoader


# -- small synthetic-data helpers, self-contained (no cross-package import) ----


def _bars(closes: list[float], start: dt.date = dt.date(2020, 1, 1)) -> pl.DataFrame:
    rows = []
    day = start
    for index, close in enumerate(closes):
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        open_price = closes[index - 1] if index else close
        rows.append(
            {
                "date": day,
                "instrument": "AAA",
                "open": open_price,
                "high": max(open_price, close) * 1.001,
                "low": min(open_price, close) * 0.999,
                "close": close,
                "volume": 1e6,
            }
        )
        day += dt.timedelta(days=1)
    return pl.DataFrame(rows).sort(["date", "instrument"])


def _trend_prices(n_bars: int, per_bar_return: float, start: float = 100.0) -> list[float]:
    prices = [start]
    for _ in range(n_bars - 1):
        prices.append(prices[-1] * (1.0 + per_bar_return))
    return prices


def _era(prices: list[float], resolved):
    """One backtest over an oscillating flat/long/long/flat signal — enough
    round trips to compute win rate and average trade, frictionless so the
    result is hand-verifiable."""
    panel = PricePanel.from_frame(_bars(prices))
    pattern = [0.0, 1.0, 1.0, 0.0]
    signals = np.array([pattern[i % 4] for i in range(len(prices))]).reshape(-1, 1)
    result = run_backtest(panel, signals, resolved, cost_multiplier=0.0)
    trades = extract_trades(result)
    return result, trades


def _replay_result(resolved, baseline, paper, *, weak_regimes=frozenset(), current_regime=None):
    baseline_result, baseline_trades = baseline
    paper_result, paper_trades = paper
    return monitoring.ReplayResult(
        resolved=resolved,
        baseline_trades=baseline_trades,
        paper_trades=paper_trades,
        baseline_result=baseline_result,
        paper_result=paper_result,
        weak_regimes=weak_regimes,
        current_regime=current_regime,
        regime_by_date={},
    )


def _deployment(baseline, resolved, **overrides):
    baseline_result, baseline_trades = baseline
    metrics = compute_metrics(baseline_result, baseline_trades, resolved.periods_per_year)
    row = dict(
        capital_minor_units=1_000_000,
        currency="INR",
        expected_sharpe=metrics.sharpe,
        expected_max_dd=metrics.max_drawdown,
        expected_win_rate=metrics.win_rate,
        expected_avg_trade=metrics.avg_trade_return,
        trades_completed=0,
        trades_required=10,
        regimes_required=["trending", "sideways", "high_vol", "low_vol"],
        regimes_observed=[],
    )
    row.update(overrides)
    return row


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def _verdict(level: str = "green", signals_tripped: tuple[str, ...] = ()) -> monitoring.HealthVerdict:
    return monitoring.HealthVerdict(
        level=level,
        live_sharpe=1.0,
        live_max_dd=0.1,
        live_win_rate=0.6,
        live_profit_factor=1.5,
        sharpe_zscore=0.0,
        win_rate_zscore=0.0,
        avg_trade_zscore=0.0,
        loss_distribution_pvalue=0.5,
        current_regime="trending",
        regime_historically_weak=False,
        slippage_deviation=0.0,
        missed_fill_rate=0.0,
        verdict_reason="ok",
        recommended_action="continue",
        trade_count=10,
        signals_tripped=signals_tripped,
    )


@pytest.fixture(scope="module")
def resolved():
    return ProfileLoader().resolve("nse_equity", "daily", "cash_equity")


# -- risk_breach -----------------------------------------------------------------


def test_risk_breach_none_when_healthy():
    equity = np.array([1.0, 1.02, 1.05, 1.03, 1.06])
    assert monitoring.risk_breach(equity, _settings()) is None


def test_risk_breach_empty_equity_returns_none():
    assert monitoring.risk_breach(np.array([]), _settings()) is None


def test_risk_breach_finds_cumulative_loss_breach():
    settings = _settings(risk_max_loss_pct=0.20, risk_max_drawdown_pct=0.90)
    # 1 - 0.79 = 0.21 > 0.20 at index 3; nothing trips earlier.
    equity = np.array([1.0, 0.90, 0.85, 0.79, 0.50])
    assert monitoring.risk_breach(equity, settings) == 3


def test_risk_breach_finds_drawdown_breach():
    settings = _settings(risk_max_loss_pct=0.90, risk_max_drawdown_pct=0.20)
    # peak = 1.1 at index 1; drawdown at index 2 is 1-0.9/1.1=0.182 (no trip);
    # at index 3, 1-0.85/1.1=0.227 > 0.20 (trips).
    equity = np.array([1.0, 1.1, 0.9, 0.85, 0.8])
    assert monitoring.risk_breach(equity, settings) == 3


def test_risk_breach_boundary_is_strict_greater_than():
    """Exactly at the limit does not trip — only past it."""
    settings = _settings(risk_max_loss_pct=0.20, risk_max_drawdown_pct=0.90)
    equity = np.array([1.0, 0.80])  # exactly 20% loss
    assert monitoring.risk_breach(equity, settings) is None


# -- health_verdict ----------------------------------------------------------------


def test_health_verdict_green_when_paper_matches_baseline(resolved):
    baseline = _era(_trend_prices(24, 0.01), resolved)
    paper = _era(_trend_prices(24, 0.01), resolved)
    result = _replay_result(resolved, baseline, paper)
    deployment = _deployment(baseline, resolved)
    settings = _settings(health_min_trades_for_verdict=3)

    verdict = monitoring.health_verdict(result, deployment, settings)

    assert verdict.trade_count >= 3
    assert verdict.level == "green"
    assert verdict.recommended_action == "continue"


def test_health_verdict_red_when_paper_degrades(resolved):
    baseline = _era(_trend_prices(24, 0.01), resolved)
    paper = _era(_trend_prices(24, -0.01), resolved)
    result = _replay_result(resolved, baseline, paper)
    deployment = _deployment(baseline, resolved)
    settings = _settings(health_min_trades_for_verdict=3)

    verdict = monitoring.health_verdict(result, deployment, settings)

    assert verdict.trade_count >= 3
    assert verdict.level == "red"
    assert verdict.recommended_action == "stop"
    assert verdict.sharpe_zscore is not None and verdict.sharpe_zscore < 0.0


def test_health_verdict_insufficient_trades_forces_green(resolved):
    baseline = _era(_trend_prices(24, 0.01), resolved)
    paper = _era(_trend_prices(4, -0.01), resolved)  # too few round trips
    result = _replay_result(resolved, baseline, paper)
    deployment = _deployment(baseline, resolved)
    settings = _settings(health_min_trades_for_verdict=20)

    verdict = monitoring.health_verdict(result, deployment, settings)

    assert verdict.level == "green"
    assert verdict.sharpe_zscore is None  # no verdict computed, not a green one that happens to be 0
    assert "below" in verdict.verdict_reason


def test_health_verdict_demotes_in_a_historically_weak_regime(resolved):
    """The done-when: the identical degradation reads differently depending
    on whether the current regime is one this strategy's own baseline shows
    it struggles in — normal losing period vs behaviour that has changed."""
    baseline = _era(_trend_prices(24, 0.01), resolved)
    paper = _era(_trend_prices(24, -0.01), resolved)
    settings = _settings(health_min_trades_for_verdict=3)
    deployment = _deployment(baseline, resolved)

    undemoted = monitoring.health_verdict(
        _replay_result(resolved, baseline, paper, weak_regimes=frozenset(), current_regime="crisis"),
        deployment,
        settings,
    )
    demoted = monitoring.health_verdict(
        _replay_result(resolved, baseline, paper, weak_regimes=frozenset({"crisis"}), current_regime="crisis"),
        deployment,
        settings,
    )

    assert undemoted.level == "red"
    assert demoted.level == "orange"
    assert demoted.regime_historically_weak is True
    assert "demoted" in demoted.verdict_reason


# -- promotion_gate ------------------------------------------------------------


def test_promotion_gate_passes_when_all_five_conditions_hold():
    deployment = dict(
        trades_completed=100,
        trades_required=100,
        regimes_required=["trending", "sideways"],
        regimes_observed=["trending", "sideways", "high_vol"],
    )
    gate = monitoring.promotion_gate(deployment, _verdict(level="green"))
    assert gate.passed is True
    assert gate.reasons == ()


def test_promotion_gate_fails_on_trade_count_alone_even_if_everything_else_passes():
    """PRD §9.3: trade count alone has never been sufficient."""
    deployment = dict(
        trades_completed=5,
        trades_required=100,
        regimes_required=["trending"],
        regimes_observed=["trending"],
    )
    gate = monitoring.promotion_gate(deployment, _verdict(level="green"))
    assert gate.passed is False
    assert gate.reasons == ("trade count",)


def test_promotion_gate_fails_on_missing_regime_coverage():
    deployment = dict(
        trades_completed=100,
        trades_required=100,
        regimes_required=["trending", "sideways"],
        regimes_observed=["trending"],
    )
    gate = monitoring.promotion_gate(deployment, _verdict(level="green"))
    assert gate.passed is False
    assert "regime coverage" in gate.reasons


def test_promotion_gate_fails_when_not_fully_green():
    deployment = dict(
        trades_completed=100,
        trades_required=100,
        regimes_required=["trending"],
        regimes_observed=["trending"],
    )
    gate = monitoring.promotion_gate(deployment, _verdict(level="yellow"))
    assert gate.passed is False
    assert "health checks" in gate.reasons
    # "not red" is still true at yellow — deviation_ok and health_ok answer
    # genuinely different questions, per PRD §9.3's two separate bullets.
    assert gate.deviation_ok is True


def test_promotion_gate_fails_on_execution_quality():
    deployment = dict(
        trades_completed=100,
        trades_required=100,
        regimes_required=["trending"],
        regimes_observed=["trending"],
    )
    gate = monitoring.promotion_gate(deployment, _verdict(level="green", signals_tripped=("slippage",)))
    assert gate.passed is False
    assert "execution quality" in gate.reasons


if __name__ == "__main__":
    # ponytail: the smallest runnable self-check, independent of pytest.
    r = ProfileLoader().resolve("nse_equity", "daily", "cash_equity")
    base = _era(_trend_prices(24, 0.01), r)
    good = _era(_trend_prices(24, 0.01), r)
    bad = _era(_trend_prices(24, -0.01), r)
    s = _settings(health_min_trades_for_verdict=3)
    d = _deployment(base, r)
    assert monitoring.health_verdict(_replay_result(r, base, good), d, s).level == "green"
    assert monitoring.health_verdict(_replay_result(r, base, bad), d, s).level == "red"
    assert monitoring.risk_breach(np.array([1.0, 0.5]), _settings(risk_max_loss_pct=0.1)) == 1
    print("aqrl.monitoring self-check OK")

"""Corporate-action adjustment — the golden tests.

The failure this prevents: a 1:2 split on unadjusted data reads as a **-50% move
that never occurred**, and every price-based indicator downstream is computed on
a lie. Across ~50 instruments over 25 years there are hundreds of such events.
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from aqrl.data.adjustment import (
    AdjustmentError,
    UnverifiedActionError,
    adjust,
    cumulative_factors,
    dividend_ratio,
    ratio_for,
)

EX_DATE = dt.date(2020, 6, 15)


def _series(prices: list[float], volumes: list[float] | None = None) -> pl.DataFrame:
    dates = [EX_DATE - dt.timedelta(days=3 - i) for i in range(len(prices))]
    return pl.DataFrame(
        {
            "date": dates,
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": volumes or [100.0] * len(prices),
        }
    )


def _action(action_type: str = "split", ratio: float = 0.5, verified: bool = True) -> dict:
    return {
        "instrument": "X",
        "action_type": action_type,
        "ex_date": EX_DATE.isoformat(),
        "ratio": ratio,
        "verified_by": "human" if verified else None,
    }


@pytest.mark.parametrize(
    "action_type,terms,expected",
    [
        ("split", "1:2", 0.5),      # one share becomes two
        ("split", "1:5", 0.2),
        ("bonus", "1:1", 0.5),      # one free per one held
        ("bonus", "1:2", 2 / 3),    # one free per two held
        ("consolidation", "5:1", 5.0),  # reverse split; prices rise
    ],
)
def test_ratio_conventions(action_type, terms, expected):
    assert ratio_for(action_type, terms) == pytest.approx(expected)


def test_dividend_ratio():
    assert dividend_ratio(2.0, 100.0) == pytest.approx(0.98)


def test_split_removes_the_phantom_gap():
    # 1000, 1010, 1020 | split | 515, 520 — a continuous ~1%/day series.
    bars = _series([1000.0, 1010.0, 1020.0, 515.0, 520.0])
    raw_returns = bars["close"].pct_change().to_list()
    assert raw_returns[3] < -0.49, "fixture should contain the phantom gap"

    adjusted = adjust(bars, [_action()])
    returns = adjusted["close"].pct_change().to_list()
    assert returns[3] == pytest.approx(0.009804, abs=1e-6)
    assert all(0.0 < r < 0.02 for r in returns[1:])


def test_volume_is_adjusted_inversely():
    """A 1:2 split doubles the share count. The most commonly forgotten half."""
    bars = _series([1000.0, 1010.0, 1020.0, 515.0, 520.0], volumes=[100.0] * 5)
    adjusted = adjust(bars, [_action()])
    assert adjusted["volume"].to_list() == [200.0, 200.0, 200.0, 100.0, 100.0]


def test_ratios_are_preserved_exactly():
    """Adjustment is harmless for ratio-based logic (TRD §14.2c)."""
    bars = _series([1000.0, 1010.0, 1020.0, 515.0, 520.0])
    before = bars["close"].to_list()
    after = adjust(bars, [_action()])["close"].to_list()
    assert after[1] / after[0] == pytest.approx(before[1] / before[0])


def test_the_ex_date_bar_itself_is_not_adjusted():
    """The interval is strictly `ex_date > bar`: the ex-date bar is already post-action."""
    bars = _series([1000.0, 1010.0, 1020.0, 515.0, 520.0])
    factors = adjust(bars, [_action()])["adjustment_factor"].to_list()
    assert factors == [0.5, 0.5, 0.5, 1.0, 1.0]


def test_multiple_actions_compound():
    bars = _series([100.0, 100.0, 100.0, 100.0, 100.0])
    actions = [
        {**_action(ratio=0.5), "ex_date": "2020-06-14"},
        {**_action(ratio=0.5), "ex_date": "2020-06-15"},
    ]
    factors = cumulative_factors(bars["date"].to_list(), actions)
    # Bars before both actions carry 0.5 * 0.5.
    assert factors[0] == pytest.approx(0.25)
    assert factors[-1] == pytest.approx(1.0)


def test_unverified_actions_are_refused():
    """Backend-Schema §12: unverified actions must not silently affect prices."""
    bars = _series([1000.0, 1010.0, 1020.0, 515.0, 520.0])
    with pytest.raises(UnverifiedActionError, match="unverified"):
        adjust(bars, [_action(verified=False)])


def test_unverified_actions_can_be_applied_explicitly():
    bars = _series([1000.0, 1010.0, 1020.0, 515.0, 520.0])
    adjusted = adjust(bars, [_action(verified=False)], allow_unverified=True)
    assert adjusted["adjustment_factor"].to_list()[0] == 0.5


def test_dividends_only_affect_the_total_return_method():
    bars = _series([100.0] * 5)
    dividend = [_action("dividend", ratio=0.99)]
    assert adjust(bars, dividend, "back_ratio_price")["adjustment_factor"][0] == 1.0
    assert adjust(bars, dividend, "back_ratio_total_return")["adjustment_factor"][0] == 0.99


def test_method_none_is_a_no_op():
    bars = _series([1000.0, 1010.0, 1020.0, 515.0, 520.0])
    adjusted = adjust(bars, [_action()], "none")
    assert adjusted["close"].to_list() == bars["close"].to_list()


def test_input_frame_is_not_mutated():
    bars = _series([1000.0, 1010.0, 1020.0, 515.0, 520.0])
    original = bars["close"].to_list()
    adjust(bars, [_action()])
    assert bars["close"].to_list() == original


def test_empty_action_list_leaves_prices_untouched():
    bars = _series([100.0, 101.0])
    adjusted = adjust(bars, [])
    assert adjusted["close"].to_list() == [100.0, 101.0]
    assert adjusted["adjustment_factor"].to_list() == [1.0, 1.0]


def test_non_positive_ratio_is_rejected():
    bars = _series([100.0, 101.0])
    with pytest.raises(AdjustmentError, match="positive"):
        adjust(bars, [_action(ratio=0.0)])


def test_missing_date_column_is_reported_clearly():
    with pytest.raises(AdjustmentError, match="no 'date' column"):
        adjust(pl.DataFrame({"close": [1.0]}), [])

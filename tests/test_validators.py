"""Validators — the safety net for what adjustment cannot catch.

A missing corporate action is invisible to the adjustment pipeline itself: it
produces a perfectly clean adjusted series that is silently wrong by 50%. The
unexplained-jump validator, running on RAW prices, is the only thing that sees it.
"""
from __future__ import annotations

import datetime as dt

import polars as pl

from aqrl.data.validators import (
    GapValidator,
    StalePriceValidator,
    UnexplainedJumpValidator,
    UniverseTooNarrowValidator,
    ValidationContext,
    ZeroVolumeValidator,
    run_validators,
)

from .conftest import make_bars


def _context(bars: pl.DataFrame, market, actions=None, **options) -> ValidationContext:
    return ValidationContext(bars=bars, market=market, actions=actions or [], options=options)


def test_unexplained_jump_is_flagged(nse_market):
    bars = make_bars({"TCS": 2000.0}, events={"TCS": (dt.date(2020, 3, 2), 0.65)})
    flags = UnexplainedJumpValidator().validate(_context(bars, nse_market))
    assert len(flags) == 1
    assert flags[0].flag_type == "unexplained_jump"
    assert flags[0].observed_value < -0.30
    assert flags[0].threshold == 0.20


def test_a_jump_with_a_matching_action_is_not_flagged(nse_market):
    bars = make_bars({"REL": 1000.0}, events={"REL": (dt.date(2020, 3, 2), 0.5)})
    actions = [
        {
            "instrument": "REL",
            "action_type": "split",
            "ex_date": "2020-03-02",
            "ratio": 0.5,
            "verified_by": "human",
        }
    ]
    assert UnexplainedJumpValidator().validate(_context(bars, nse_market, actions)) == []


def test_unverified_actions_still_explain_a_jump(nse_market):
    """An unverified record explains the jump even though it cannot move prices.

    Treating it as unknown would bury real missing-action cases in false alarms.
    """
    bars = make_bars({"REL": 1000.0}, events={"REL": (dt.date(2020, 3, 2), 0.5)})
    actions = [
        {
            "instrument": "REL",
            "action_type": "split",
            "ex_date": "2020-03-02",
            "ratio": 0.5,
            "verified_by": None,
        }
    ]
    assert UnexplainedJumpValidator().validate(_context(bars, nse_market, actions)) == []


def test_the_action_match_window_absorbs_a_one_day_discrepancy(nse_market):
    bars = make_bars({"REL": 1000.0}, events={"REL": (dt.date(2020, 3, 3), 0.5)})
    actions = [
        {
            "instrument": "REL",
            "action_type": "split",
            "ex_date": "2020-03-02",  # filed one day earlier than the bar reflects
            "ratio": 0.5,
            "verified_by": "human",
        }
    ]
    assert UnexplainedJumpValidator().validate(_context(bars, nse_market, actions)) == []


def test_a_clean_series_raises_nothing(nse_market):
    bars = make_bars({"A": 100.0, "B": 200.0})
    assert run_validators(_context(bars, nse_market)) == []


def test_the_threshold_comes_from_the_market_profile(nse_market):
    bars = make_bars({"X": 100.0}, events={"X": (dt.date(2020, 3, 2), 0.85)})  # -15%
    assert UnexplainedJumpValidator().validate(_context(bars, nse_market)) == []
    tighter = _context(bars, nse_market, unexplained_jump_threshold=0.10)
    assert len(UnexplainedJumpValidator().validate(tighter)) == 1


def test_universe_too_narrow_flags_survivorship(nse_market):
    """50 tickers across 25 years of a 50-name index is the bias, not tidy data."""
    instruments = {f"S{i:02d}": 100.0 + i for i in range(50)}
    bars = make_bars(instruments, start=dt.date(2000, 1, 3), days=9000)
    flags = UniverseTooNarrowValidator().validate(_context(bars, nse_market, index_size=50))
    assert len(flags) == 1
    assert flags[0].observed_value == 50
    assert flags[0].threshold > 90  # ~100-150 expected over that span


def test_universe_with_realistic_turnover_is_not_flagged(nse_market):
    instruments = {f"S{i:03d}": 100.0 + i for i in range(120)}
    bars = make_bars(instruments, start=dt.date(2000, 1, 3), days=400)
    context = _context(bars, nse_market, index_size=50, min_span_years=0.5)
    assert UniverseTooNarrowValidator().validate(context) == []


def test_short_spans_are_not_judged_for_narrowness(nse_market):
    instruments = {f"S{i:02d}": 100.0 for i in range(50)}
    bars = make_bars(instruments, days=200)
    assert UniverseTooNarrowValidator().validate(_context(bars, nse_market, index_size=50)) == []


def test_zero_volume_is_flagged(nse_market):
    bars = make_bars({"X": 100.0}, days=40)
    bars = bars.with_columns(
        pl.when(pl.col("date") == bars["date"][5]).then(0.0).otherwise(pl.col("volume")).alias("volume")
    )
    flags = ZeroVolumeValidator().validate(_context(bars, nse_market))
    assert len(flags) == 1 and flags[0].flag_type == "zero_volume"


def test_stale_price_is_flagged(nse_market):
    bars = make_bars({"X": 100.0}, days=40, daily_drift=0.0)  # never moves
    flags = StalePriceValidator().validate(_context(bars, nse_market))
    assert len(flags) == 1
    assert flags[0].observed_value > 5


def test_gap_is_flagged(nse_market):
    bars = make_bars({"X": 100.0}, days=60)
    kept = bars.filter(
        (pl.col("date") < dt.date(2020, 1, 20)) | (pl.col("date") > dt.date(2020, 2, 10))
    )
    flags = GapValidator().validate(_context(kept, nse_market))
    assert len(flags) == 1 and flags[0].observed_value > 5


def test_normal_weekend_gaps_are_not_flagged(nse_market):
    bars = make_bars({"X": 100.0}, days=60)
    assert GapValidator().validate(_context(bars, nse_market)) == []

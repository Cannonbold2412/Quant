"""Fixtures for the engine suites.

Profiles come from the real `profiles/` tree, as everywhere else in this
codebase — the shipped YAML *is* the contract, and a mock version of it would
test nothing. Prices are synthetic and constructed, because a known-answer test
needs an answer known in advance.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from aqrl.eval.panel import PricePanel
from aqrl.profiles import ProfileLoader
from aqrl.profiles.models import ResolvedProfile

TRADING_DAYS_PER_YEAR = 252


def bars(
    closes: dict[str, list[float]] | list[float],
    start: dt.date = dt.date(2020, 1, 1),
    opens: dict[str, list[float]] | None = None,
    volume: float = 1e6,
) -> pl.DataFrame:
    """A long OHLCV frame from explicit closes — one weekday per bar.

    `open` defaults to the previous close, so `bar_close` and `next_open` fills
    agree unless a test deliberately separates them.
    """
    if isinstance(closes, list):
        closes = {"AAA": closes}

    rows = []
    for instrument, series in closes.items():
        day = start
        for index, close in enumerate(series):
            while day.weekday() >= 5:
                day += dt.timedelta(days=1)
            if opens is not None:
                open_price = opens[instrument][index]
            else:
                open_price = series[index - 1] if index else close
            rows.append(
                {
                    "date": day,
                    "instrument": instrument,
                    "open": open_price,
                    "high": max(open_price, close) * 1.001,
                    "low": min(open_price, close) * 0.999,
                    "close": close,
                    "volume": volume,
                }
            )
            day += dt.timedelta(days=1)
    return pl.DataFrame(rows).sort(["date", "instrument"])


def random_walk_bars(
    n_bars: int,
    instruments: int = 1,
    seed: int = 0,
    drift: float = 0.0,
    volatility: float = 0.01,
    start: dt.date = dt.date(2000, 1, 3),
) -> pl.DataFrame:
    """A geometric random walk — no edge by construction unless drift says so."""
    rng = np.random.default_rng(seed)
    closes = {}
    for index in range(instruments):
        returns = rng.normal(drift, volatility, n_bars)
        closes[f"INS{index:02d}"] = list(100.0 * np.cumprod(1.0 + returns))
    return bars(closes, start=start)


def panel_of(frame: pl.DataFrame) -> PricePanel:
    return PricePanel.from_frame(frame)


def with_fill_model(resolved: ResolvedProfile, model: str) -> ResolvedProfile:
    """A copy of a resolved profile under a different fill model.

    **Test-only.** The copy keeps the original's `timeframe_profile_hash`, which
    would be a lie in production — profiles are immutable and a changed profile
    is a new version with a new hash (`profiles/models.py`). Here it exists so a
    fill model can be varied without shipping a YAML file per test.
    """
    return resolved.model_copy(
        update={"timeframe": resolved.timeframe.model_copy(update={"fill_model": model})}
    )


@pytest.fixture
def resolved() -> ResolvedProfile:
    """nse_equity × daily × cash_equity — the one fully-specified profile.

    Note its fill model is `next_open`, not `bar_close`: the shipped daily
    profile takes the conservative fill, so the engine's default path forgoes
    the overnight gap on every entry.
    """
    return ProfileLoader().resolve("nse_equity", "daily", "cash_equity")


@pytest.fixture
def bars_frame():
    return bars


@pytest.fixture
def walk_frame():
    return random_walk_bars

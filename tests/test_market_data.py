"""Tests for the read-only market-data tree loader."""
from __future__ import annotations

from datetime import date

import pandas as pd
import polars as pl
import pytest

from aqrl.data import (
    BarFile,
    MarketDataError,
    available_instruments,
    load_ohlcv,
)


def _bar_frame(dates: list[str], base_close: float = 100.0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "Date": [pd.Timestamp(value).date() for value in dates],
            "Ticker": ["TEST"] * len(dates),
            "open": [base_close] * len(dates),
            "high": [base_close + 1.0] * len(dates),
            "low": [base_close - 1.0] * len(dates),
            "close": [base_close] * len(dates),
            "volume": [0] * len(dates),
        }
    )


@pytest.fixture()
def tree(tmp_path):
    forex = tmp_path / "forex" / "daily"
    forex.mkdir(parents=True)
    commodities = tmp_path / "commodities" / "daily"
    commodities.mkdir(parents=True)

    _bar_frame(
        [
            "2024-01-02", "2024-01-03", "2024-01-04",
            "2024-01-05", "2024-01-08", "2024-01-09",
        ]
    ).write_parquet(forex / "TESTUSD.parquet")
    _bar_frame(["2024-02-01", "2024-02-02"], base_close=2000.0).write_parquet(
        commodities / "GOLD.parquet"
    )
    return tmp_path


def test_available_instruments_discovers_the_tree(tree):
    entries = available_instruments(data_root=tree)
    assert [(entry.ticker, entry.asset_class, entry.timeframe) for entry in entries] == [
        ("GOLD", "commodities", "daily"),
        ("TESTUSD", "forex", "daily"),
    ]


def test_available_instruments_filters_by_asset_class(tree):
    assert [entry.ticker for entry in available_instruments(tree, asset_class="forex")] == [
        "TESTUSD"
    ]
    assert available_instruments(tree, asset_class="equity") == []


def test_load_returns_date_indexed_sorted_frame(tree):
    df = load_ohlcv("testusd", data_root=tree)
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.name == "date"
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.is_monotonic_increasing
    assert df.index[0] == pd.Timestamp("2024-01-02")
    assert df.index[-1] == pd.Timestamp("2024-01-09")


def test_load_respects_start_end_filters(tree):
    df = load_ohlcv("TESTUSD", start=date(2024, 1, 4), end="2024-01-08", data_root=tree)
    assert list(df.index) == [pd.Timestamp("2024-01-04"), pd.Timestamp("2024-01-05"),
                              pd.Timestamp("2024-01-08")]


def test_unknown_ticker_raises_and_lists_known(tree):
    with pytest.raises(MarketDataError, match="unknown instrument 'NOPE'"):
        load_ohlcv("NOPE", data_root=tree)


def test_empty_range_raises_rather_than_returning_silently_empty(tree):
    with pytest.raises(MarketDataError, match="no bars"):
        load_ohlcv("TESTUSD", start="2030-01-01", data_root=tree)


def test_missing_required_column_raises(tree):
    bad = _bar_frame(["2024-01-02"]).drop("close")
    bad.write_parquet(tree / "forex" / "daily" / "BAD.parquet")
    with pytest.raises(MarketDataError, match="missing required column"):
        load_ohlcv("BAD", data_root=tree)


def test_duplicate_dates_raise(tree):
    frame = pl.concat([_bar_frame(["2024-03-01"]), _bar_frame(["2024-03-01"])])
    frame.write_parquet(tree / "forex" / "daily" / "DUP.parquet")
    with pytest.raises(MarketDataError, match="duplicate dates"):
        load_ohlcv("DUP", data_root=tree)


def test_duplicate_ticker_across_tree_raises(tmp_path):
    for folder in ("forex/daily", "commodities/daily"):
        path = tmp_path / folder
        path.mkdir(parents=True)
        _bar_frame(["2024-01-02"]).write_parquet(path / "TWIN.parquet")
    with pytest.raises(MarketDataError, match="appears twice"):
        available_instruments(data_root=tmp_path)


def test_missing_root_raises(tmp_path):
    with pytest.raises(MarketDataError, match="does not exist"):
        available_instruments(data_root=tmp_path / "nowhere")


def test_bar_file_is_frozen():
    entry = BarFile("X", "forex", "daily", __import__("pathlib").Path("."))
    with pytest.raises(AttributeError):
        entry.ticker = "Y"  # type: ignore[misc]

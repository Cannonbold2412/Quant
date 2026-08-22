"""Loader for the local market-data tree: `{data_root}/{asset_class}/{timeframe}/{TICKER}.parquet`.

Same principle as the rest of the package: **read only**. Files in this tree
are raw source bars (forex / commodities / indices, daily), never rewritten
by anything here. Every frame returned is validated, deduplicated, sorted and
indexed by date so the research loop's backtest contract (`df["close"]` on a
DatetimeIndex) holds without further ceremony.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd
import polars as pl

from ..config import Settings, get_settings

REQUIRED_COLUMNS = ("date", "open", "high", "low", "close")
OPTIONAL_COLUMNS = ("volume",)


class MarketDataError(ValueError):
    """A market-data file is missing, malformed, or an instrument unknown."""


@dataclass(frozen=True)
class BarFile:
    """One instrument's bar file, located by asset class and timeframe."""

    ticker: str
    asset_class: str
    timeframe: str
    path: Path


def _resolve_root(data_root: Path | str | None) -> Path:
    if data_root is not None:
        return Path(data_root)
    settings: Settings = get_settings()
    return settings.data_root


@lru_cache(maxsize=8)
def _index(root: Path) -> dict[str, BarFile]:
    """Ticker -> BarFile across the whole tree. Cached per root."""
    if not root.is_dir():
        raise MarketDataError(f"data_root {root} does not exist")
    found: dict[str, BarFile] = {}
    for path in sorted(root.glob("*/*/*.parquet")):
        asset_class, timeframe = path.parts[-3], path.parts[-2]
        entry = BarFile(path.stem.upper(), asset_class, timeframe, path)
        existing = found.get(entry.ticker)
        if existing is not None:
            raise MarketDataError(
                f"ticker {entry.ticker!r} appears twice: {existing.path} and {path}. "
                "Tickers must be unique across the tree."
            )
        found[entry.ticker] = entry
    return found


def available_instruments(
    data_root: Path | str | None = None,
    *,
    asset_class: str | None = None,
    timeframe: str | None = None,
) -> list[BarFile]:
    """Every instrument in the tree, optionally filtered by class/timeframe."""
    entries = list(_index(_resolve_root(data_root)).values())
    if asset_class is not None:
        entries = [entry for entry in entries if entry.asset_class == asset_class]
    if timeframe is not None:
        entries = [entry for entry in entries if entry.timeframe == timeframe]
    return sorted(entries, key=lambda entry: entry.ticker)


def _to_date(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def load_ohlcv(
    ticker: str,
    start: str | date | None = None,
    end: str | date | None = None,
    *,
    data_root: Path | str | None = None,
) -> pd.DataFrame:
    """One instrument's OHLCV as a pandas frame indexed by date.

    Columns are `open`, `high`, `low`, `close` plus `volume` when present.
    Rows outside [start, end] (inclusive on both ends) are dropped. Raises
    `MarketDataError` for an unknown ticker or a malformed file — never
    returns an empty or unsorted frame silently.
    """
    entry = _index(_resolve_root(data_root)).get(ticker.upper())
    if entry is None:
        known = ", ".join(sorted(_index(_resolve_root(data_root))))
        raise MarketDataError(f"unknown instrument {ticker!r} (known: {known})")

    frame = pl.read_parquet(entry.path)
    frame = frame.rename({column: column.lower() for column in frame.columns})
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise MarketDataError(f"{entry.path} is missing required column(s) {missing}")

    if frame["date"].is_duplicated().any():
        raise MarketDataError(f"{entry.path} has duplicate dates; fix the source file")

    frame = frame.with_columns(pl.col("date").cast(pl.Date)).sort("date")
    if start is not None:
        frame = frame.filter(pl.col("date") >= _to_date(start))
    if end is not None:
        frame = frame.filter(pl.col("date") <= _to_date(end))

    keep = [column for column in (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS) if column in frame.columns]
    result = frame.select(keep).to_pandas()
    if result.empty:
        raise MarketDataError(
            f"{ticker!r} has no bars in [{start}, {end}] "
            f"(file spans {frame['date'].min()} .. {frame['date'].max()})"
            if start is not None or end is not None
            else f"{entry.path} contains no rows"
        )
    result.index = pd.DatetimeIndex(pd.to_datetime(result.pop("date")), name="date")
    return result

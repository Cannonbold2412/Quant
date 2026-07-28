"""Shared fixtures.

Every test gets its own database and data root, so nothing touches the
developer's working `aqrl.db` and tests cannot leak state into each other.
Profiles are read from the real `profiles/` tree — they are the shipped
contract, and testing against a mock version of them would test nothing.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from aqrl.config import PROJECT_ROOT, Settings
from aqrl.db import connect, migrate
from aqrl.profiles import ProfileLoader

PROFILES_DIR = PROJECT_ROOT / "profiles"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path / "data",
        profiles_dir=PROFILES_DIR,
        log_level="WARNING",
    )


@pytest.fixture
def conn(settings: Settings):
    connection = connect(settings.db_path)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def loader() -> ProfileLoader:
    return ProfileLoader(PROFILES_DIR)


@pytest.fixture
def nse_market(loader: ProfileLoader):
    return loader.load_market("nse_equity")


def make_bars(
    instruments: dict[str, float],
    start: dt.date = dt.date(2020, 1, 1),
    days: int = 200,
    daily_drift: float = 0.0004,
    events: dict[str, tuple[dt.date, float]] | None = None,
) -> pl.DataFrame:
    """A clean synthetic OHLCV frame, optionally with a price event.

    `events` maps instrument -> (date, multiplier) applied from that date
    onwards, which is how a split or an unexplained drop is simulated.
    """
    events = events or {}
    rows = []
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for instrument, base in instruments.items():
            price = base * (1.0 + daily_drift) ** offset
            if instrument in events:
                event_date, multiplier = events[instrument]
                if day >= event_date:
                    price *= multiplier
            rows.append(
                {
                    "date": day,
                    "instrument": instrument,
                    "open": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                    "volume": 10_000.0,
                }
            )
    return pl.DataFrame(rows)


@pytest.fixture
def bars_factory():
    return make_bars

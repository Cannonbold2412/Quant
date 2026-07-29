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


# -- operator fixtures ----------------------------------------------------------
#
# The operator suites are parametrized over the WHOLE registry, so a new
# operator is covered the moment it is registered and cannot be added untested.
# That only works if inputs and parameter draws can be produced generically —
# hence these two helpers rather than per-operator fixtures.


def operator_inputs(operator, n: int = 400, seed: int = 0) -> dict:
    """Plausible arrays for whatever ports an operator declares.

    Deliberately a *random walk* rather than a smooth curve: a monotone series
    hides sign errors, and a constant one hides division-by-zero handling.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0, 0.01, n)
    close = 100.0 * np.cumprod(1.0 + returns)

    available = {
        "series": close,
        "close": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "volume": np.abs(rng.normal(1e4, 2e3, n)),
        "returns": returns,
        # Alternates between long, flat and short so stops and time limits are
        # actually exercised rather than sitting in one state for the whole run.
        "position": np.sign(np.sin(np.arange(n) / 7.0)),
        "fast": close,
        "slow": close * 0.995,
        "a": np.sign(np.sin(np.arange(n) / 5.0)),
        "b": np.sign(np.cos(np.arange(n) / 11.0)),
    }
    if operator.category == "portfolio":
        # (bars x instruments): the one family whose arrays are 2-D.
        panel = np.column_stack(
            [rng.normal(0.0, 0.01 * (1 + i * 0.3), n) for i in range(6)]
        )
        available["returns"] = panel
        available["series"] = panel

    return {port: available[port] for port in operator.inputs}


def param_draws(operator) -> list[dict]:
    """The defaults, plus scaled variants that survive their declared ranges.

    Testing defaults alone would leave the range logic — and any parameter-
    dependent look-ahead, such as an off-by-one window — unexercised. Draws that
    violate a declared range or a cross-parameter rule are skipped rather than
    clamped, because a clamped draw silently tests something other than what it
    names.
    """
    draws = [operator.bind()]
    for factor in (0.5, 1.7):
        candidate = {}
        for spec in operator.params:
            if spec.kind == "int":
                candidate[spec.name] = max(int(spec.default * factor), 1)
            elif spec.kind == "float":
                candidate[spec.name] = spec.default * factor
        if not candidate:
            continue
        try:
            bound = operator.bind(**candidate)
        except ValueError:
            continue  # out of range or rejected by validate_params — fine
        if bound not in draws:
            draws.append(bound)
    return draws


@pytest.fixture
def inputs_factory():
    return operator_inputs

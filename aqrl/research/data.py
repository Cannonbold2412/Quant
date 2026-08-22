"""data.py — snapshots, calendars, cost models, the vault. READ ONLY.

Agent permission (TRD §2.1): **read only.** No function here ever accepts a
write. `strategy.py` is the only file meant to change; this one is meant to
be trusted the same way `evaluate.py` is.

**Real data, explicitly.** The backing series is the local market-data tree
(`data/{asset_class}/{timeframe}/{TICKER}.parquet`) via `aqrl.data.market_data`
— global-macro daily bars (forex, commodities, indices), not the synthetic
NIFTY proxy this module served while no real history was collected. The
synthetic generators remain in `synthetic_data.py` and still back null-world;
they are no longer what `get_ohlcv` returns.

The default instrument is USA500 — a price-only index series, so long-only
results overstate by the dividend yield (see the market profile's notes).

The vault (TRD §15.2) locks the most recent two years of every series. The
research loop's only entry point, `get_ohlcv`, always checks it — there is no
parameter on `get_ohlcv` that opens it. See `vault.py` for the one path
that can (`open_vault_for_promotion`), which this module deliberately does
not import.
"""
from __future__ import annotations

import pandas as pd

from aqrl.data import load_ohlcv
from aqrl.profiles import ProfileLoader

from .cost_models import CostModel, get_cost_model
from .vault import VaultConfig, VaultGuard, VaultLockedError  # noqa: F401  (re-exported for callers)

MARKET = "global_macro"
ASSET_CLASS = "cfd"
TIMEFRAME = "daily"
INSTRUMENT = "USA500"

# Derived from the market calendar and the bar size, never hardcoded (TRD
# §13.2): "one wrong value makes every Sharpe in the database fiction." For
# nse_equity x daily this resolves to 252, but it resolves to 94,500 for 1-minute
# bars on the same market and 525,600 for 1-minute bars on a 24/7 venue — which
# is exactly why the constant that used to sit here was a latent bug.
_RESOLVED = ProfileLoader().resolve(MARKET, TIMEFRAME, ASSET_CLASS)
PERIODS_PER_YEAR = _RESOLVED.periods_per_year
MARKET_PROFILE_HASH = _RESOLVED.market_profile_hash
TIMEFRAME_PROFILE_HASH = _RESOLVED.timeframe_profile_hash
COST_MODEL_HASH = _RESOLVED.cost_model_hash

_FULL_DF = load_ohlcv(INSTRUMENT)

_VAULT_YEARS_LOCKED = 2
_vault_locked_start = _FULL_DF.index[-1] - pd.DateOffset(years=_VAULT_YEARS_LOCKED) + pd.Timedelta(days=1)
VAULT_CONFIG = VaultConfig(locked_start=_vault_locked_start, locked_end=_FULL_DF.index[-1])
_guard = VaultGuard(VAULT_CONFIG)  # private — evaluate.py/strategy.py cannot reach this object


def full_history_range() -> tuple[pd.Timestamp, pd.Timestamp]:
    return _FULL_DF.index[0], _FULL_DF.index[-1]


def research_range() -> tuple[pd.Timestamp, pd.Timestamp]:
    """The span the loop can actually use — everything before the vault."""
    return _FULL_DF.index[0], VAULT_CONFIG.locked_start - pd.Timedelta(days=1)


def get_ohlcv(instrument: str, start: str | pd.Timestamp, end: str | pd.Timestamp) -> pd.DataFrame:
    """The loop's one read path. Raises `VaultLockedError` if the requested
    range or instrument touches the vault — always, unconditionally."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    _guard.check(instrument, start, end)
    frame = load_ohlcv(instrument, start.date(), end.date())
    return frame.loc[start:end].copy()


def cost_model() -> CostModel:
    return get_cost_model(MARKET, ASSET_CLASS)

"""data.py — snapshots, calendars, cost models, the vault. READ ONLY.

Agent permission (TRD §2.1): **read only.** No function here ever accepts a
write. `strategy.py` is the only file meant to change; this one is meant to
be trusted the same way `evaluate.py` is.

**Placeholder data, explicitly.** README/TRD §14 are explicit that real
NIFTY-50 history isn't collected yet — point-in-time index membership needs
price data for the ~100-150 ever-members, not today's 50, and that data
collection is still open. Rather than pretend that's solved, this file
serves a single clearly-labeled **synthetic, index-level proxy series** —
consistent with the docs' own statement that index-level research can
proceed while equity-level survivorship bias is unresolved. No result
produced against this data should be mistaken for a real NIFTY-50 finding.

The vault (TRD §15.2) locks the most recent two years of this series. The
research loop's only entry point, `get_ohlcv`, always checks it — there is no
parameter on `get_ohlcv` that opens it. See `_lib/vault.py` for the one path
that can (`open_vault_for_promotion`), which this module deliberately does
not import.
"""
from __future__ import annotations

import pandas as pd

from aqrl.profiles import ProfileLoader

from ._lib.cost_models import CostModel, get_cost_model
from ._lib.synthetic_data import synthetic_ohlcv
from ._lib.vault import VaultConfig, VaultGuard, VaultLockedError  # noqa: F401  (re-exported for callers)

MARKET = "nse_equity"
ASSET_CLASS = "cash_equity"
TIMEFRAME = "daily"
INSTRUMENT = "SYN_NSE_INDEX_PROXY"

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

_N_YEARS = 20
_N_DAYS = 252 * _N_YEARS
_START = "2005-01-03"
_SEED = 42

_FULL_DF = synthetic_ohlcv(_N_DAYS, start=_START, seed=_SEED)

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
    if instrument != INSTRUMENT:
        raise KeyError(f"unknown instrument: {instrument}")
    return _FULL_DF.loc[start:end].copy()


def cost_model() -> CostModel:
    return get_cost_model(MARKET, ASSET_CLASS)

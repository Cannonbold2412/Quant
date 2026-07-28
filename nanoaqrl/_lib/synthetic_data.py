"""Synthetic OHLCV + null-world generators.

**Placeholder for real market data.** The docs are explicit that real NIFTY-50
history (survivorship, corporate actions) is not yet collected (README, TRD
§14) — Indian-equity results cannot reach live capital until that exists.
Everything here is clearly-labeled synthetic data, sufficient to exercise the
nanoAQRL loop end to end and to run null-world calibration honestly (TRD
§15.3), but it proves nothing about real markets.

Null-world generators produce data with **no alpha by construction**:
permuted returns, block bootstrap, and synthetic fat-tailed paths — the three
named explicitly in TRD §15.3.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def _returns_to_ohlcv(
    returns: np.ndarray,
    start: str,
    base_price: float = 100.0,
    seed: int | None = None,
) -> pd.DataFrame:
    """Turn a daily-return series into a plausible OHLCV frame."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=len(returns))
    close = base_price * np.cumprod(1.0 + returns)
    open_ = np.empty_like(close)
    open_[0] = base_price
    open_[1:] = close[:-1]
    intraday_noise = rng.uniform(0.001, 0.006, size=len(returns))
    high = np.maximum(open_, close) * (1.0 + intraday_noise)
    low = np.minimum(open_, close) * (1.0 - intraday_noise)
    volume = rng.integers(1_000_00, 5_000_00, size=len(returns)).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


def synthetic_ohlcv(
    n_days: int,
    start: str = "2000-01-03",
    annual_drift: float = 0.08,
    annual_vol: float = 0.22,
    dof: float = 5.0,
    seed: int = 0,
    base_price: float = 100.0,
) -> pd.DataFrame:
    """Baseline synthetic instrument: fat-tailed (Student-t) daily returns
    with a configurable drift/vol, matched to plausible equity-index
    magnitudes. Not real data — see module docstring."""
    rng = np.random.default_rng(seed)
    daily_mu = annual_drift / TRADING_DAYS_PER_YEAR
    daily_sigma = annual_vol / np.sqrt(TRADING_DAYS_PER_YEAR)
    t_draws = rng.standard_t(dof, size=n_days)
    # scale Student-t draws to the target daily sigma (var of t_dof is dof/(dof-2))
    t_draws *= daily_sigma / np.sqrt(dof / (dof - 2.0))
    returns = daily_mu + t_draws
    return _returns_to_ohlcv(returns, start=start, base_price=base_price, seed=seed)


def permuted_returns_ohlcv(
    n_days: int, start: str = "2000-01-03", seed: int = 0, base_price: float = 100.0
) -> pd.DataFrame:
    """Null-world generator #1: shuffle a real-ish return series so any
    autocorrelation/predictability is destroyed while the marginal
    distribution is preserved exactly."""
    base = synthetic_ohlcv(n_days, start=start, seed=seed, base_price=base_price)
    returns = base["close"].pct_change().dropna().to_numpy()
    rng = np.random.default_rng(seed + 1)
    shuffled = returns.copy()
    rng.shuffle(shuffled)
    return _returns_to_ohlcv(shuffled, start=start, base_price=base_price, seed=seed + 1)


def block_bootstrap_ohlcv(
    n_days: int,
    block_size: int = 20,
    start: str = "2000-01-03",
    seed: int = 0,
    base_price: float = 100.0,
) -> pd.DataFrame:
    """Null-world generator #2: resample fixed-length blocks of returns with
    replacement. Preserves short-horizon autocorrelation structure (so it is
    a *harder* null than a full shuffle) while destroying any genuine
    long-horizon predictive relationship, since blocks are stitched from
    random, non-contiguous points in time."""
    base = synthetic_ohlcv(n_days + block_size, start=start, seed=seed, base_price=base_price)
    returns = base["close"].pct_change().dropna().to_numpy()
    rng = np.random.default_rng(seed + 2)
    n_blocks = int(np.ceil(n_days / block_size))
    starts = rng.integers(0, max(len(returns) - block_size, 1), size=n_blocks)
    chunks = [returns[s : s + block_size] for s in starts]
    stitched = np.concatenate(chunks)[:n_days]
    return _returns_to_ohlcv(stitched, start=start, base_price=base_price, seed=seed + 2)


def synthetic_path_ohlcv(
    n_days: int,
    start: str = "2000-01-03",
    annual_vol: float = 0.22,
    dof: float = 4.0,
    seed: int = 0,
    base_price: float = 100.0,
) -> pd.DataFrame:
    """Null-world generator #3: a synthetic fat-tailed path with **zero
    drift** and matched volatility — no alpha by construction, independent
    of any real-data draw (unlike the two generators above, which start from
    a real-ish base series)."""
    rng = np.random.default_rng(seed + 3)
    daily_sigma = annual_vol / np.sqrt(TRADING_DAYS_PER_YEAR)
    t_draws = rng.standard_t(dof, size=n_days)
    t_draws *= daily_sigma / np.sqrt(dof / (dof - 2.0))
    return _returns_to_ohlcv(t_draws, start=start, base_price=base_price, seed=seed + 3)

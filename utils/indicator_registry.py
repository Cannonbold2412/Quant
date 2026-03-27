from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


INDICATOR_REGISTRY: dict[str, dict[str, Any]] = {
    "sma": {
        "aliases": ["sma", "simple moving average"],
        "family": "trend",
        "description": "Simple moving average used for trend direction and crossover signals.",
        "default_parameters": {"fast_window": 10, "slow_window": 20},
        "supported_in_engine": True,
    },
    "ema": {
        "aliases": ["ema", "exponential moving average"],
        "family": "trend",
        "description": "Exponential moving average used for responsive trend and pullback signals.",
        "default_parameters": {"fast_window": 12, "slow_window": 26},
        "supported_in_engine": True,
    },
    "rsi": {
        "aliases": ["rsi", "relative strength index"],
        "family": "oscillator",
        "description": "Momentum oscillator for overbought, oversold, and confirmation logic.",
        "default_parameters": {"period": 14, "upper_threshold": 70, "lower_threshold": 30},
        "supported_in_engine": True,
    },
    "macd": {
        "aliases": ["macd"],
        "family": "momentum",
        "description": "MACD trend and momentum confirmation using line and signal crossovers.",
        "default_parameters": {"fast_window": 12, "slow_window": 26, "signal_window": 9},
        "supported_in_engine": True,
    },
    "atr": {
        "aliases": ["atr", "average true range"],
        "family": "volatility",
        "description": "Volatility measure used for breakout and expansion filters.",
        "default_parameters": {"period": 14, "expansion_window": 20},
        "supported_in_engine": True,
    },
    "vwap": {
        "aliases": ["vwap", "volume weighted average price"],
        "family": "volume",
        "description": "Intraday volume-weighted price anchor used for bias confirmation.",
        "default_parameters": {},
        "supported_in_engine": True,
    },
    "bollinger_bands": {
        "aliases": ["bollinger", "bollinger bands", "bbands"],
        "family": "band",
        "description": "Volatility bands used for mean-reversion and breakout logic.",
        "default_parameters": {"window": 20, "num_std": 2},
        "supported_in_engine": True,
    },
    "stochastic": {
        "aliases": ["stochastic", "stoch", "stochastic oscillator"],
        "family": "oscillator",
        "description": "Oscillator used for momentum turns and overbought or oversold entries.",
        "default_parameters": {"k_period": 14, "d_period": 3},
        "supported_in_engine": True,
    },
    "adx": {
        "aliases": ["adx", "average directional index"],
        "family": "trend_strength",
        "description": "Trend-strength indicator used to validate directional setups.",
        "default_parameters": {"period": 14, "threshold": 20},
        "supported_in_engine": True,
    },
    "momentum": {
        "aliases": ["momentum", "mom"],
        "family": "momentum",
        "description": "Rate-of-change style momentum signal for directional confirmation.",
        "default_parameters": {"period": 10},
        "supported_in_engine": True,
    },
    "roc": {
        "aliases": ["roc", "rate of change"],
        "family": "momentum",
        "description": "Percentage rate-of-change momentum confirmation.",
        "default_parameters": {"period": 10},
        "supported_in_engine": True,
    },
    "volume_sma": {
        "aliases": ["volume sma", "volume moving average", "volume_ma"],
        "family": "volume",
        "description": "Average volume filter used to confirm breakouts and participation.",
        "default_parameters": {"window": 20, "multiplier": 1.1},
        "supported_in_engine": True,
    },
    "obv": {
        "aliases": ["obv", "on balance volume"],
        "family": "volume",
        "description": "Cumulative volume-flow indicator used for confirmation.",
        "default_parameters": {"signal_window": 10},
        "supported_in_engine": True,
    },
    "cci": {
        "aliases": ["cci", "commodity channel index"],
        "family": "oscillator",
        "description": "Oscillator for momentum extremes and reversal setups.",
        "default_parameters": {"period": 20},
        "supported_in_engine": True,
    },
    "jma": {
        "aliases": ["jma", "jurik moving average", "jurgen moving average"],
        "family": "trend",
        "description": "Jurik-style moving average and normalized JMA variants used for signal generation.",
        "default_parameters": {"smooth_length": 14},
        "supported_in_engine": False,
    },
}


ALIAS_TO_CANONICAL: dict[str, str] = {}
for canonical_name, payload in INDICATOR_REGISTRY.items():
    ALIAS_TO_CANONICAL[canonical_name] = canonical_name
    for alias in payload["aliases"]:
        ALIAS_TO_CANONICAL[alias.lower()] = canonical_name


BOOTSTRAP_INDICATORS = [
    "ema",
    "sma",
    "rsi",
    "macd",
    "atr",
    "vwap",
    "bollinger_bands",
    "stochastic",
    "adx",
    "momentum",
    "roc",
    "volume_sma",
]


def normalize_indicator_name(name: str | None) -> str | None:
    if not name:
        return None
    lowered = name.strip().lower()
    return ALIAS_TO_CANONICAL.get(lowered)


def canonicalize_indicator_name(name: str | None) -> str | None:
    if not name:
        return None
    canonical = normalize_indicator_name(name)
    if canonical:
        return canonical
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or None


def indicator_metadata(name: str) -> dict[str, Any]:
    if name in INDICATOR_REGISTRY:
        return deepcopy(INDICATOR_REGISTRY[name])
    readable = name.replace("_", " ").strip() or "custom indicator"
    return {
        "aliases": [name, readable],
        "family": "custom",
        "description": f"Custom indicator extracted from notebook code: {readable}.",
        "default_parameters": {},
        "supported_in_engine": False,
    }


def supported_indicator_names() -> list[str]:
    return [name for name, payload in INDICATOR_REGISTRY.items() if payload.get("supported_in_engine")]

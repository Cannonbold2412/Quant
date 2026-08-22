"""The data layer — snapshots, load-time adjustment, point-in-time universe.

The single principle: **raw prices are immutable and every adjustment happens
at load time** (TRD §14.2a). Nothing in this package rewrites a price file.

This package is `read only` from the research loop's perspective (TRD §2.1). A
strategy may read it and must never edit it — which is what stops a strategy
quietly widening its own universe or softening its own costs.
"""

from .adjustment import (
    AdjustmentError,
    UnverifiedActionError,
    adjust,
    cumulative_factors,
    dividend_ratio,
    ratio_for,
)
from .market_data import BarFile, MarketDataError, available_instruments, load_ohlcv
from .snapshots import SnapshotError, SnapshotManager
from .universe import UniverseError, UniverseResolver

__all__ = [
    "AdjustmentError",
    "BarFile",
    "MarketDataError",
    "SnapshotError",
    "SnapshotManager",
    "UniverseError",
    "UniverseResolver",
    "UnverifiedActionError",
    "adjust",
    "available_instruments",
    "cumulative_factors",
    "dividend_ratio",
    "load_ohlcv",
    "ratio_for",
]

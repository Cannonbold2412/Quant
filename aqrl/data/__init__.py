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
from .snapshots import SnapshotError, SnapshotManager
from .universe import UniverseError, UniverseResolver

__all__ = [
    "adjust",
    "cumulative_factors",
    "ratio_for",
    "dividend_ratio",
    "AdjustmentError",
    "UnverifiedActionError",
    "SnapshotManager",
    "SnapshotError",
    "UniverseResolver",
    "UniverseError",
]

"""Market, timeframe and cost profiles — the evaluation contract's parameters.

TRD §6.7 calls this the *fixed evaluation contract*: data snapshot, cost model,
walk-forward configuration and test protocol held constant across a campaign,
because **every change partitions the result history**. Content hashing is what
makes that partition visible instead of silent.
"""

from .loader import ProfileError, ProfileLoader, default_loader, hash_profile
from .models import (
    Calendar,
    Constraints,
    CostModel,
    MarketProfile,
    ResolvedProfile,
    TimeframeProfile,
    Universe,
    check_periods_per_year,
    profile_payload,
    resolve_periods_per_year,
)

__all__ = [
    "Calendar",
    "Constraints",
    "CostModel",
    "MarketProfile",
    "ProfileError",
    "ProfileLoader",
    "ResolvedProfile",
    "TimeframeProfile",
    "Universe",
    "check_periods_per_year",
    "default_loader",
    "hash_profile",
    "profile_payload",
    "resolve_periods_per_year",
]

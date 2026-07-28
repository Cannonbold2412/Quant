"""The validator registry.

Every market runs the core set. `MARKET_VALIDATORS` is where a market adds its
own — TRD §6.5's principle applied to data quality: **extra gates, not a
separate engine**, so results stay comparable across markets.
"""
from __future__ import annotations

from .base import Flag, ValidationContext, Validator
from .core import (
    GapValidator,
    StalePriceValidator,
    UniverseTooNarrowValidator,
    UnexplainedJumpValidator,
    ZeroVolumeValidator,
)

# Order matters only for readability of the resulting flag list.
CORE_VALIDATORS: tuple[Validator, ...] = (
    UnexplainedJumpValidator(),
    UniverseTooNarrowValidator(),
    ZeroVolumeValidator(),
    StalePriceValidator(),
    GapValidator(),
)

# Per-market additions, appended to the core set.
MARKET_VALIDATORS: dict[str, tuple[Validator, ...]] = {}


def validators_for(market: str) -> tuple[Validator, ...]:
    return CORE_VALIDATORS + MARKET_VALIDATORS.get(market, ())


def run_validators(context: ValidationContext) -> list[Flag]:
    """Run every validator applicable to this market and collect the flags."""
    flags: list[Flag] = []
    for validator in validators_for(context.market.name):
        flags.extend(validator.validate(context))
    return flags


__all__ = [
    "Flag",
    "ValidationContext",
    "Validator",
    "CORE_VALIDATORS",
    "MARKET_VALIDATORS",
    "validators_for",
    "run_validators",
    "UnexplainedJumpValidator",
    "UniverseTooNarrowValidator",
    "ZeroVolumeValidator",
    "StalePriceValidator",
    "GapValidator",
]

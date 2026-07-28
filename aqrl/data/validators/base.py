"""Validator protocol and context.

Validators run **at ingest, not at experiment time** (TRD §13.1). By the time a
strategy is being scored it is far too late for "actually, that -50% move was a
split we never recorded" to be a useful discovery.

Each validator returns `Flag`s. A flag is a *question for a human*, not a
verdict: every one is either a real market event or a data error, and a person
must say which. A snapshot with unresolved flags cannot be marked valid and the
scheduler will not dispatch experiments against it (TRD §14.4).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import polars as pl

from ...profiles.models import MarketProfile


@dataclass(frozen=True)
class Flag:
    """One thing a human needs to adjudicate."""

    flag_type: str
    detail: str
    instrument: str | None = None
    bar_date: str | None = None
    observed_value: float | None = None
    threshold: float | None = None


@dataclass
class ValidationContext:
    """Everything a validator may look at.

    `bars` are **raw and unadjusted**, deliberately. The unexplained-jump check
    only works on raw prices — that is the whole point of it: adjustment is only
    as good as the corporate-actions data behind it, so the one thing that
    catches a *missing* action is seeing the jump the adjustment failed to
    remove.
    """

    bars: pl.DataFrame
    market: MarketProfile
    # Every known action, verified or not. An unverified record still explains a
    # jump, so excluding them here would produce false alarms.
    actions: Sequence[Mapping[str, Any]] = field(default_factory=list)
    date_column: str = "date"
    instrument_column: str = "instrument"
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def instruments(self) -> list[str]:
        if self.instrument_column not in self.bars.columns:
            return []
        return sorted(self.bars[self.instrument_column].unique().to_list())


class Validator(Protocol):
    """A named check over one snapshot's raw bars."""

    name: str
    flag_type: str

    def validate(self, context: ValidationContext) -> list[Flag]: ...

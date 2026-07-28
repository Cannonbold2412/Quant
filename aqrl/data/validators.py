"""Ingest-time validators — the safety net for what adjustment cannot catch.

The asymmetry that makes this module necessary (TRD §14.4): the adjustment
pipeline can only apply the corporate actions it was *told about*. A **missing**
action is invisible to it — the pipeline runs happily and emits a perfectly
clean adjusted series that is silently wrong by 50% for one instrument, forever.
Nothing downstream can detect that. Null-world calibration cannot detect it
either, because the null generator inherits the same corrupted inputs (TRD
§14.1).

The unexplained-jump validator, running on **raw** prices *before* adjustment,
is the only thing in the system that sees it.

**These run at ingest, not at experiment time** (TRD §13.1). A flag is a
question for a human, not a warning to skim: every one is either a real market
event or a data error, and `data_validation_flags.resolution` stays `pending`
until somebody says which. A snapshot with pending flags cannot be marked valid
and refuses to load — which is what stops a corrupted series quietly becoming a
research finding.
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from ..profiles.models import MarketProfile

__all__ = [
    "GapValidator",
    "StalePriceValidator",
    "UnexplainedJumpValidator",
    "UniverseTooNarrowValidator",
    "ValidationContext",
    "ValidationFlag",
    "Validator",
    "ZeroVolumeValidator",
    "default_validators",
    "run_validators",
]

# A corporate action filed within this many calendar days of an observed jump is
# taken to explain it. Filing dates and the bar that actually reflects the event
# routinely differ by a day, and over a weekend by three; demanding an exact
# match would bury the real missing-action cases under false alarms.
ACTION_MATCH_WINDOW_DAYS = 3

# Fraction of an index's names replaced per year. NIFTY-50 turns over roughly
# 2-4 constituents annually, so ~6% is the honest middle. Over 2000-2025 this
# puts the expected ever-member count at ~124, matching the 100-150 range TRD
# §14.3a states independently.
INDEX_TURNOVER_PER_YEAR = 0.06

# Below this span, constituent turnover says nothing: a six-month window of a
# 50-name index legitimately contains 50 names.
MIN_SPAN_YEARS_FOR_NARROWNESS = 2.0

# Consecutive identical closes beyond this are a data feed repeating itself, not
# a market. Genuine limit-locked instruments exist, which is why it is a flag
# for a human rather than a rejection.
MAX_STALE_RUN_BARS = 5

# Calendar days between consecutive bars. A weekend is 3, a long weekend 4; only
# beyond that is a gap evidence of missing data rather than a closed exchange.
MAX_GAP_DAYS = 5

DAYS_PER_YEAR = 365.25


@dataclass(frozen=True)
class ValidationFlag:
    """One question for a human, shaped like a `data_validation_flags` row."""

    flag_type: str
    detail: str
    instrument: str | None = None
    bar_date: str | None = None
    observed_value: float | None = None
    threshold: float | None = None

    def as_row(self) -> dict[str, Any]:
        """The kwargs `ValidationFlagRepository.insert` expects."""
        return {
            "flag_type": self.flag_type,
            "instrument": self.instrument,
            "bar_date": self.bar_date,
            "observed_value": self.observed_value,
            "threshold": self.threshold,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ValidationContext:
    """Everything a validator is allowed to look at.

    `bars` are **raw** — unadjusted, exactly as ingested. That is the whole
    point: adjusted prices have had the phantom gaps removed, including the ones
    that were never real.
    """

    bars: pl.DataFrame
    market: MarketProfile
    actions: Sequence[Mapping[str, Any]] = field(default_factory=list)
    options: Mapping[str, Any] = field(default_factory=dict)

    def option(self, name: str, default: Any = None) -> Any:
        """An explicit override, falling back to the profile-driven default."""
        return self.options.get(name, default)

    def per_instrument(self) -> list[tuple[str, pl.DataFrame]]:
        """Split into date-sorted frames, one per instrument.

        A frame with no `instrument` column is a single unnamed series — the
        shape `adjust()` works on, and the shape most tests use.
        """
        if "instrument" not in self.bars.columns:
            return [("", self.bars.sort("date"))]
        return [
            (str(name[0]), group.sort("date"))
            for name, group in self.bars.sort("date").group_by(["instrument"], maintain_order=True)
        ]


class Validator(ABC):
    """One check. Returns flags; never raises on bad data, never mutates."""

    flag_type: str

    @abstractmethod
    def validate(self, context: ValidationContext) -> list[ValidationFlag]: ...


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


# -- the one that matters most -------------------------------------------------


class UnexplainedJumpValidator(Validator):
    """|return| beyond the profile threshold with no corporate action to match.

    The threshold is **profile-driven** (`MarketProfile.unexplained_jump_threshold`)
    because a 20% move means something different on a circuit-limited Indian
    equity — where it is barely legal — than on spot crypto, where it is
    Tuesday. NSE's widest single-instrument band is 20%, so the default sits
    exactly at the edge of what a legal session can produce.

    An action explains a jump if it lands on the same instrument within
    `ACTION_MATCH_WINDOW_DAYS`. **Unverified actions count as explanations**
    even though they are refused the power to move prices: a filed-but-unchecked
    split is evidence that the jump is real, and treating it as unknown would
    bury genuine missing-action cases under noise. Verification gates the
    *adjustment*, not the *explanation*.
    """

    flag_type = "unexplained_jump"

    def validate(self, context: ValidationContext) -> list[ValidationFlag]:
        threshold = float(
            context.option("unexplained_jump_threshold", context.market.unexplained_jump_threshold)
        )
        window = dt.timedelta(days=int(context.option("action_match_window_days", ACTION_MATCH_WINDOW_DAYS)))

        by_instrument: dict[str, list[dt.date]] = {}
        for action in context.actions:
            ex_date = _as_date(action.get("ex_date"))
            if ex_date is not None:
                by_instrument.setdefault(str(action.get("instrument", "")), []).append(ex_date)

        flags: list[ValidationFlag] = []
        for instrument, frame in context.per_instrument():
            if "close" not in frame.columns or frame.height < 2:
                continue
            returns = frame["close"].pct_change().to_list()
            dates = frame["date"].to_list()
            explained = by_instrument.get(instrument, [])

            for value, raw_date in zip(returns[1:], dates[1:], strict=True):
                if value is None or abs(value) <= threshold:
                    continue
                bar_date = _as_date(raw_date)
                if bar_date is not None and any(
                    abs(bar_date - ex_date) <= window for ex_date in explained
                ):
                    continue
                flags.append(
                    ValidationFlag(
                        flag_type=self.flag_type,
                        instrument=instrument or None,
                        bar_date=bar_date.isoformat() if bar_date else None,
                        observed_value=float(value),
                        threshold=threshold,
                        detail=(
                            f"{value:+.2%} move with no corporate action within "
                            f"{window.days} day(s). Either a missing split/bonus — which would "
                            f"corrupt this instrument's entire series — or a genuine move."
                        ),
                    )
                )
        return flags


# -- survivorship ---------------------------------------------------------------


class UniverseTooNarrowValidator(Validator):
    """An index snapshot holding only today's constituents is the bias itself.

    ~50 tickers across 25 years of a 50-name index is not tidy data; it is
    survivorship bias in its purest form (TRD §14.3). The companies that left
    are exactly the ones whose losses are invisible, so a *suspiciously clean*
    universe is the symptom to look for.

    Skipped below `min_span_years` — over a few months a 50-name index really
    does contain 50 names, and flagging that would be noise.
    """

    flag_type = "universe_too_narrow"

    def validate(self, context: ValidationContext) -> list[ValidationFlag]:
        index_size = context.option("index_size")
        if not index_size or "date" not in context.bars.columns or context.bars.height == 0:
            return []

        dates = [d for d in (_as_date(v) for v in context.bars["date"].to_list()) if d is not None]
        if not dates:
            return []
        span_years = (max(dates) - min(dates)).days / DAYS_PER_YEAR
        if span_years < float(context.option("min_span_years", MIN_SPAN_YEARS_FOR_NARROWNESS)):
            return []

        turnover = float(context.option("index_turnover_per_year", INDEX_TURNOVER_PER_YEAR))
        expected = float(index_size) * (1.0 + turnover * span_years)

        observed = (
            context.bars["instrument"].n_unique() if "instrument" in context.bars.columns else 1
        )
        if observed >= expected:
            return []

        return [
            ValidationFlag(
                flag_type=self.flag_type,
                observed_value=float(observed),
                threshold=expected,
                detail=(
                    f"{observed} distinct instruments across {span_years:.1f} years of a "
                    f"{int(index_size)}-name index; ~{expected:.0f} ever-members expected at "
                    f"{turnover:.0%} annual turnover. The missing names are the ones that left — "
                    "their losses are what survivorship bias hides."
                ),
            )
        ]


# -- feed-quality checks --------------------------------------------------------


def _runs(values: Sequence[Any], is_flagged) -> list[tuple[int, int]]:
    """Contiguous `(start_index, length)` runs where `is_flagged` holds.

    Runs rather than individual bars: a delisted instrument with a year of dead
    quotes should raise one question, not 250 identical ones.
    """
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(values):
        if is_flagged(value):
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index - start))
            start = None
    if start is not None:
        runs.append((start, len(values) - start))
    return runs


class ZeroVolumeValidator(Validator):
    """Bars that traded nothing. A halt, a delisting, or a broken feed."""

    flag_type = "zero_volume"

    def validate(self, context: ValidationContext) -> list[ValidationFlag]:
        flags: list[ValidationFlag] = []
        for instrument, frame in context.per_instrument():
            if "volume" not in frame.columns:
                continue
            volumes = frame["volume"].to_list()
            dates = frame["date"].to_list()
            for start, length in _runs(volumes, lambda v: v is not None and float(v) <= 0.0):
                bar_date = _as_date(dates[start])
                flags.append(
                    ValidationFlag(
                        flag_type=self.flag_type,
                        instrument=instrument or None,
                        bar_date=bar_date.isoformat() if bar_date else None,
                        observed_value=float(length),
                        threshold=0.0,
                        detail=(
                            f"{length} consecutive bar(s) with zero volume from {bar_date}. "
                            "A fill at a price nothing traded at is not a fill."
                        ),
                    )
                )
        return flags


class StalePriceValidator(Validator):
    """A close that repeats for longer than a market plausibly stands still.

    Genuine limit-locked instruments exist, so this is a question rather than a
    rejection — but a repeating feed produces zero variance, and zero variance
    produces an infinite Sharpe.
    """

    flag_type = "stale_price"

    def validate(self, context: ValidationContext) -> list[ValidationFlag]:
        limit = int(context.option("max_stale_run_bars", MAX_STALE_RUN_BARS))
        flags: list[ValidationFlag] = []

        for instrument, frame in context.per_instrument():
            if "close" not in frame.columns or frame.height < 2:
                continue
            closes = frame["close"].to_list()
            dates = frame["date"].to_list()

            run_start, run_length = 0, 1
            for index in range(1, len(closes)):
                if closes[index] is not None and closes[index] == closes[index - 1]:
                    run_length += 1
                    continue
                if run_length > limit:
                    flags.append(self._flag(instrument, dates[run_start], run_length, limit))
                run_start, run_length = index, 1
            if run_length > limit:
                flags.append(self._flag(instrument, dates[run_start], run_length, limit))
        return flags

    def _flag(self, instrument: str, raw_date: Any, length: int, limit: int) -> ValidationFlag:
        bar_date = _as_date(raw_date)
        return ValidationFlag(
            flag_type=self.flag_type,
            instrument=instrument or None,
            bar_date=bar_date.isoformat() if bar_date else None,
            observed_value=float(length),
            threshold=float(limit),
            detail=(
                f"close unchanged for {length} consecutive bars from {bar_date}. "
                "Zero variance produces an infinite Sharpe, so this cannot pass silently."
            ),
        )


class GapValidator(Validator):
    """Missing stretches of history.

    A weekend is three calendar days and a long weekend four, so only beyond
    `max_gap_days` is a gap evidence of *missing data* rather than a closed
    exchange. Holidays make the exact boundary market-specific; the flag exists
    so a human decides.
    """

    flag_type = "gap"

    def validate(self, context: ValidationContext) -> list[ValidationFlag]:
        limit = int(context.option("max_gap_days", MAX_GAP_DAYS))
        flags: list[ValidationFlag] = []

        for instrument, frame in context.per_instrument():
            dates = [d for d in (_as_date(v) for v in frame["date"].to_list()) if d is not None]
            for previous, current in zip(dates, dates[1:], strict=False):
                span = (current - previous).days
                if span <= limit:
                    continue
                flags.append(
                    ValidationFlag(
                        flag_type=self.flag_type,
                        instrument=instrument or None,
                        bar_date=current.isoformat(),
                        observed_value=float(span),
                        threshold=float(limit),
                        detail=(
                            f"{span} calendar days between {previous} and {current}, "
                            "longer than a holiday weekend explains."
                        ),
                    )
                )
        return flags


# -- the suite -----------------------------------------------------------------


def default_validators() -> list[Validator]:
    """Every validator, jump-first because it is the one that catches the
    corruption nothing else in the system can see."""
    return [
        UnexplainedJumpValidator(),
        UniverseTooNarrowValidator(),
        ZeroVolumeValidator(),
        StalePriceValidator(),
        GapValidator(),
    ]


def run_validators(
    context: ValidationContext,
    validators: Sequence[Validator] | None = None,
) -> list[ValidationFlag]:
    """Run the suite at ingest and collect every question it raises."""
    flags: list[ValidationFlag] = []
    for validator in validators if validators is not None else default_validators():
        flags.extend(validator.validate(context))
    return flags

"""The validators every market runs (TRD §14.4).

| Flag                  | Catches                                              |
|-----------------------|------------------------------------------------------|
| `unexplained_jump`    | a genuine event, or a **missing** corporate action   |
| `universe_too_narrow` | survivorship bias hiding as tidy data                |
| `zero_volume`         | untradeable bars presented as tradeable              |
| `stale_price`         | a feed that stopped updating                         |
| `gap`                 | missing sessions                                     |
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from .base import Flag, ValidationContext


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:19]).date()


def _per_instrument(context: ValidationContext) -> pl.DataFrame:
    """Bars sorted per instrument, with a synthetic instrument column if absent."""
    frame = context.bars
    if context.instrument_column not in frame.columns:
        frame = frame.with_columns(pl.lit("__single__").alias(context.instrument_column))
    return frame.sort([context.instrument_column, context.date_column])


class UnexplainedJumpValidator:
    """|return| beyond the market's threshold with no matching corporate action.

    **This is the only thing that catches a missing corporate action.** The
    adjustment pipeline cannot detect what it was never told about: a split with
    no row in `corporate_actions` produces a perfectly clean adjusted series
    that is silently wrong by 50%.

    Runs on RAW prices, before adjustment, and matches against *all* known
    actions including unverified ones — an unverified record still explains the
    jump, so treating it as unknown would bury the real cases in noise.

    The +/-1 day window absorbs the ordinary disagreement between an ex-date as
    filed and the first bar that reflects it.
    """

    name = "unexplained_jump"
    flag_type = "unexplained_jump"

    def validate(self, context: ValidationContext) -> list[Flag]:
        frame = _per_instrument(context)
        if frame.height < 2 or "close" not in frame.columns:
            return []

        threshold = float(
            context.options.get("unexplained_jump_threshold", context.market.unexplained_jump_threshold)
        )
        window = timedelta(days=int(context.options.get("action_match_window_days", 1)))

        action_dates: dict[str, list[date]] = {}
        for action in context.actions:
            action_dates.setdefault(str(action["instrument"]), []).append(_as_date(action["ex_date"]))

        instrument_column = context.instrument_column
        returns = frame.with_columns(
            (pl.col("close") / pl.col("close").shift(1).over(instrument_column) - 1.0).alias("_return")
        ).filter(pl.col("_return").abs() > threshold)

        flags: list[Flag] = []
        for row in returns.iter_rows(named=True):
            instrument = row[instrument_column]
            bar_date = _as_date(row[context.date_column])
            explained = any(
                abs((bar_date - ex_date).days) <= window.days
                for ex_date in action_dates.get(instrument, [])
            )
            if explained:
                continue
            move = float(row["_return"])
            flags.append(
                Flag(
                    flag_type=self.flag_type,
                    instrument=None if instrument == "__single__" else instrument,
                    bar_date=bar_date.isoformat(),
                    observed_value=move,
                    threshold=threshold,
                    detail=(
                        f"{move:+.2%} single-bar move against a {threshold:.0%} threshold with no "
                        "corporate action within +/-1 day. Either a genuine market event or a "
                        "missing split/bonus record."
                    ),
                )
            )
        return flags


class UniverseTooNarrowValidator:
    """Distinct instruments ~= index size across a long span (TRD §14.3c).

    *"A 25-year NIFTY-50 backtest touching exactly 50 tickers is a symptom of
    the bias, not a sign of tidy data."* Over 25 years roughly 100-150 tickers
    passed through NIFTY-50; seeing 50 means the departed ones were dropped, and
    with them every loss they took on the way out.
    """

    name = "universe_too_narrow"
    flag_type = "universe_too_narrow"

    def validate(self, context: ValidationContext) -> list[Flag]:
        index_size = context.options.get("index_size")
        if not index_size or context.instrument_column not in context.bars.columns:
            return []

        instruments = context.instruments
        observed = len(instruments)
        if observed <= 1:
            return []

        dates = context.bars[context.date_column].to_list()
        span_years = (_as_date(max(dates)) - _as_date(min(dates))).days / 365.25
        min_span = float(context.options.get("min_span_years", 3.0))
        if span_years < min_span:
            return []  # too short a span for turnover to be expected

        # Roughly 2-5 constituent changes a year; anything under half that rate
        # of accumulated turnover is suspicious.
        expected = index_size + 2.0 * span_years
        if observed >= expected * 0.5 + index_size * 0.5:
            return []

        return [
            Flag(
                flag_type=self.flag_type,
                observed_value=float(observed),
                threshold=float(expected),
                detail=(
                    f"{observed} distinct instruments over {span_years:.1f} years against an index "
                    f"size of {index_size}; ~{expected:.0f} would be expected once constituents that "
                    "left are included. Today's members projected backwards is survivorship bias."
                ),
            )
        ]


class ZeroVolumeValidator:
    """Bars reporting no trading. A price nobody could transact at is not a price."""

    name = "zero_volume"
    flag_type = "zero_volume"

    def validate(self, context: ValidationContext) -> list[Flag]:
        frame = _per_instrument(context)
        if "volume" not in frame.columns:
            return []
        max_fraction = float(context.options.get("max_zero_volume_fraction", 0.0))

        flags: list[Flag] = []
        for (instrument,), group in frame.group_by([context.instrument_column], maintain_order=True):
            zeros = int(group.filter(pl.col("volume") <= 0).height)
            if not zeros:
                continue
            fraction = zeros / group.height
            if fraction <= max_fraction:
                continue
            flags.append(
                Flag(
                    flag_type=self.flag_type,
                    instrument=None if instrument == "__single__" else str(instrument),
                    observed_value=fraction,
                    threshold=max_fraction,
                    detail=f"{zeros} of {group.height} bars have zero or negative volume ({fraction:.2%}).",
                )
            )
        return flags


class StalePriceValidator:
    """Runs of identical closes — usually a feed that stopped updating."""

    name = "stale_price"
    flag_type = "stale_price"

    def validate(self, context: ValidationContext) -> list[Flag]:
        frame = _per_instrument(context)
        if "close" not in frame.columns or frame.height < 2:
            return []
        max_run = int(context.options.get("max_stale_run", 5))

        instrument_column = context.instrument_column
        runs = (
            frame.with_columns(
                (pl.col("close") != pl.col("close").shift(1).over(instrument_column))
                .fill_null(True)
                .cum_sum()
                .over(instrument_column)
                .alias("_run")
            )
            .group_by([instrument_column, "_run"], maintain_order=True)
            .agg(
                pl.len().alias("_length"),
                pl.col(context.date_column).first().alias("_start"),
                pl.col("close").first().alias("_price"),
            )
            .filter(pl.col("_length") > max_run)
        )

        return [
            Flag(
                flag_type=self.flag_type,
                instrument=None if row[instrument_column] == "__single__" else str(row[instrument_column]),
                bar_date=_as_date(row["_start"]).isoformat(),
                observed_value=float(row["_length"]),
                threshold=float(max_run),
                detail=(
                    f"close held at {row['_price']} for {row['_length']} consecutive bars "
                    f"from {_as_date(row['_start']).isoformat()}."
                ),
            )
            for row in runs.iter_rows(named=True)
        ]


class GapValidator:
    """Missing sessions — bars absent from a series that should be continuous."""

    name = "gap"
    flag_type = "gap"

    def validate(self, context: ValidationContext) -> list[Flag]:
        frame = _per_instrument(context)
        if frame.height < 2:
            return []
        # 5 calendar days absorbs a weekend plus a holiday on a daily series.
        max_gap_days = int(context.options.get("max_gap_days", 5))

        instrument_column = context.instrument_column
        flags: list[Flag] = []
        for (instrument,), group in frame.group_by([instrument_column], maintain_order=True):
            dates = [_as_date(value) for value in group[context.date_column].to_list()]
            for previous, current in zip(dates, dates[1:]):
                gap = (current - previous).days
                if gap > max_gap_days:
                    flags.append(
                        Flag(
                            flag_type=self.flag_type,
                            instrument=None if instrument == "__single__" else str(instrument),
                            bar_date=current.isoformat(),
                            observed_value=float(gap),
                            threshold=float(max_gap_days),
                            detail=f"{gap}-day gap between {previous.isoformat()} and {current.isoformat()}.",
                        )
                    )
        return flags

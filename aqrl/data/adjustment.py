"""Corporate-action adjustment, applied AT LOAD TIME (TRD §14.2).

Source data for Indian equities is unadjusted, so splits and bonus issues
appear as violent phantom gaps: a 1:2 split halves the price overnight and
every indicator reads a **-50% move that never occurred.** Across ~50
instruments over 25 years there are hundreds of such events.

**Why this is computed rather than stored.** Back-adjusting the files once
rewrites *all* historical prices, so every new split changes the entire past,
changes the content hash, and marks every prior experiment `comparable = 0`. A
single corporate action would invalidate the archive. Instead raw OHLCV is
immutable, `corporate_actions` is append-only and versioned, and the adjusted
series is derived here on every load. A new split appends one row and bumps the
actions version; the price hash never moves (TRD §14.2a).

**Volume is adjusted inversely** — a 1:2 split doubles the share count. TRD
calls this "the most commonly forgotten half", and volume-based operators break
silently without it.

**The look-ahead caveat (TRD §14.2c).** Back-adjustment is itself mildly
forward-looking: the adjusted price shown for 2015 depends on splits that
happened in 2020. Harmless for anything ratio-based — returns, percentage
moves, crossovers, volatility — because ratios are preserved exactly. But
*contaminating for absolute price levels*, which is why absolute price
thresholds are forbidden in `program.md` and enforced at P0.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Literal

import numpy as np
import polars as pl

AdjustmentMethod = Literal["back_ratio_price", "back_ratio_total_return", "none"]

# Which action types each method applies. Dividends move a total-return series
# but not a price series, so the choice changes results and is versioned on the
# snapshot alongside the data.
PRICE_ACTIONS = frozenset({"split", "bonus", "consolidation"})
TOTAL_RETURN_ACTIONS = PRICE_ACTIONS | {"dividend"}

DEFAULT_PRICE_COLUMNS = ("open", "high", "low", "close")
DEFAULT_VOLUME_COLUMNS = ("volume",)


class AdjustmentError(ValueError):
    """The adjustment cannot be performed as specified."""


class UnverifiedActionError(AdjustmentError):
    """An unverified corporate action would have moved prices.

    Backend-Schema §12: *"unverified actions must not silently affect prices."*
    Silently applying one would let an unreviewed data-entry error rewrite an
    instrument's entire history.
    """


def ratio_for(action_type: str, terms: str) -> float:
    """Convert published terms to a back-adjustment ratio (TRD §14.2b).

    * split ``1:N``      -> ``1/N``      (1:2 means one share becomes two)
    * bonus ``a:b``      -> ``b/(a+b)``  (a free shares per b held)
    * consolidation ``N:1`` -> ``N``     (reverse split; prices rise)
    """
    try:
        left, _, right = terms.partition(":")
        first, second = float(left.strip()), float(right.strip())
    except (ValueError, AttributeError) as exc:
        raise AdjustmentError(f"cannot parse terms {terms!r} for a {action_type}") from exc
    if first <= 0 or second <= 0:
        raise AdjustmentError(f"terms {terms!r} must be positive")

    if action_type == "split":
        return first / second
    if action_type == "bonus":
        return second / (first + second)
    if action_type == "consolidation":
        return first / second
    raise AdjustmentError(f"ratio_for does not handle {action_type!r}; dividends need a price")


def dividend_ratio(dividend: float, price: float) -> float:
    """``(P - D) / P`` — total-return adjustment only."""
    if price <= 0:
        raise AdjustmentError("dividend adjustment needs a positive reference price")
    if dividend < 0 or dividend >= price:
        raise AdjustmentError(f"dividend {dividend} is not sensible against price {price}")
    return (price - dividend) / price


def _as_dates(values: Sequence[Any]) -> np.ndarray:
    parsed: list[date] = []
    for value in values:
        if isinstance(value, datetime):
            parsed.append(value.date())
        elif isinstance(value, date):
            parsed.append(value)
        else:
            parsed.append(datetime.fromisoformat(str(value)[:19]).date())
    return np.array(parsed, dtype="datetime64[D]")


def cumulative_factors(
    bar_dates: Sequence[Any],
    actions: Sequence[Mapping[str, Any]],
) -> np.ndarray:
    """The back-adjustment factor for each bar.

    Implements TRD §14.2b directly::

        cumulative_factor = 1.0
        for each bar, newest -> oldest:
            if an unapplied action has ex_date > bar.date:
                cumulative_factor *= action.ratio

    Computed as a suffix product plus a binary search rather than a Python loop,
    which is the same arithmetic in one pass. A bar *on* the ex-date is already
    post-action and takes factor 1 — the interval is strictly ``ex_date > bar``.
    """
    bars = _as_dates(bar_dates)
    if not len(actions):
        return np.ones(len(bars), dtype=float)

    ordered = sorted(actions, key=lambda a: str(a["ex_date"]))
    ex_dates = _as_dates([a["ex_date"] for a in ordered])
    ratios = np.array([float(a["ratio"]) for a in ordered], dtype=float)
    if np.any(ratios <= 0):
        raise AdjustmentError("corporate action ratios must be positive")

    # suffix[k] = product of ratios from k onwards; suffix[len] = 1.0
    suffix = np.ones(len(ratios) + 1, dtype=float)
    for index in range(len(ratios) - 1, -1, -1):
        suffix[index] = suffix[index + 1] * ratios[index]

    # Number of actions with ex_date <= bar; those are already reflected in the
    # raw price and must not be applied again.
    applied = np.searchsorted(ex_dates, bars, side="right")
    return suffix[applied]


def select_actions(
    actions: Sequence[Mapping[str, Any]],
    method: AdjustmentMethod,
    allow_unverified: bool = False,
) -> list[Mapping[str, Any]]:
    """Filter actions to those this method applies, rejecting unverified ones."""
    if method == "none":
        return []
    applicable = PRICE_ACTIONS if method == "back_ratio_price" else TOTAL_RETURN_ACTIONS

    selected = [a for a in actions if a["action_type"] in applicable]
    unverified = [a for a in selected if not a.get("verified_by")]
    if unverified and not allow_unverified:
        summary = ", ".join(f"{a['instrument']} {a['action_type']} {a['ex_date']}" for a in unverified[:5])
        raise UnverifiedActionError(
            f"{len(unverified)} unverified corporate action(s) would move prices: {summary}. "
            "Verify them (or pass allow_unverified) — an unreviewed action must not silently "
            "rewrite an instrument's history."
        )
    return selected


def adjust(
    bars: pl.DataFrame,
    actions: Sequence[Mapping[str, Any]],
    method: AdjustmentMethod = "back_ratio_price",
    *,
    date_column: str = "date",
    price_columns: Sequence[str] = DEFAULT_PRICE_COLUMNS,
    volume_columns: Sequence[str] = DEFAULT_VOLUME_COLUMNS,
    allow_unverified: bool = False,
    keep_factor: bool = True,
) -> pl.DataFrame:
    """Return `bars` back-adjusted for `actions`. The input is never mutated.

    Adds an `adjustment_factor` column so any adjusted series can be inverted
    back to the raw prints — without it, "why is this 2015 close not what the
    exchange printed?" has no answer.
    """
    if date_column not in bars.columns:
        raise AdjustmentError(f"bars have no {date_column!r} column (columns: {bars.columns})")
    if bars.is_empty():
        return bars.with_columns(pl.lit(1.0).alias("adjustment_factor")) if keep_factor else bars

    selected = select_actions(actions, method, allow_unverified=allow_unverified)
    frame = bars.sort(date_column)
    factors = cumulative_factors(frame[date_column].to_list(), selected)

    factor_series = pl.Series("adjustment_factor", factors)
    expressions = [
        (pl.col(column) * factor_series).alias(column) for column in price_columns if column in frame.columns
    ]
    # The opposite direction: a 1:2 split doubles the share count.
    expressions += [
        (pl.col(column) / factor_series).alias(column) for column in volume_columns if column in frame.columns
    ]
    if keep_factor:
        expressions.append(factor_series.alias("adjustment_factor"))

    return frame.with_columns(expressions)

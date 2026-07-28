"""Corporate-action adjustment — applied at LOAD time, never persisted.

The failure this exists to prevent is not subtle: source data for Indian
equities is **unadjusted** (TRD §14.2), so a 1:2 split reads as a −50% move that
never occurred. Every price-based indicator computed downstream — every moving
average, every volatility estimate, every breakout threshold — is then computed
on a lie. Across ~50 instruments over 25 years there are hundreds of such
events, and they do not cancel out.

**Store raw, adjust at load (TRD §14.2a).** Back-adjusting the Parquet files in
place would change `raw_content_hash` on every new corporate action, marking the
entire archive incomparable — one split invalidating years of results. So the
raw bytes are immutable and this module runs on the way out, with the actions
history carried separately as `corporate_actions_version`.

**The direction is backwards.** A factor applies to bars *strictly before* the
ex-date: on the ex-date the market price has already adjusted itself. Multiple
actions compound multiplicatively, so a bar preceding two 1:2 splits carries
0.25.

**Volume moves inversely** — a 1:2 split doubles the share count, so historical
volume must double to stay comparable. This is the most commonly forgotten half
of the operation.

Ratio conventions, as recorded on `corporate_actions.ratio`:

| Action | Terms | Ratio |
|---|---|---|
| split | 1:N — one share becomes N | 1/N |
| bonus | a:b — a free per b held | b/(a+b) |
| consolidation | N:1 — N shares become one | N |
| dividend | D per share at price P | (P−D)/P |

> ⚠️ **The look-ahead caveat nobody mentions (TRD §14.2c).** A back-adjusted
> series encodes knowledge of every future action into past prices, so an
> *absolute* price level is contaminated: "buy below ₹500" is a rule the past
> could not have evaluated. Ratios, returns and percentage distances are
> unaffected, which is why `program.md` forbids absolute price thresholds
> outright. Adjustment is mandatory; it is not free.
"""
from __future__ import annotations

import bisect
import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import polars as pl

__all__ = [
    "AdjustmentError",
    "UnverifiedActionError",
    "adjust",
    "cumulative_factors",
    "dividend_ratio",
    "ratio_for",
]

# The OHLC columns a price factor multiplies, if present.
PRICE_COLUMNS = ("open", "high", "low", "close")
# Adjusted inversely: the factor divides it.
VOLUME_COLUMNS = ("volume",)

# Only the total-return method touches dividends. A price-adjusted series answers
# "what did the chart look like?"; a total-return series answers "what did the
# holder earn?". Mixing them silently inflates every long-only backtest.
_PRICE_ACTIONS = frozenset({"split", "bonus", "consolidation"})
_TOTAL_RETURN_ACTIONS = _PRICE_ACTIONS | {"dividend"}

_ACTIONS_FOR_METHOD: dict[str, frozenset[str]] = {
    "back_ratio_price": _PRICE_ACTIONS,
    "back_ratio_total_return": _TOTAL_RETURN_ACTIONS,
    "none": frozenset(),
}


class AdjustmentError(ValueError):
    """The frame or an action is malformed — adjustment cannot proceed."""


class UnverifiedActionError(AdjustmentError):
    """An unverified action was asked to move prices (Backend-Schema §12).

    `corporate_actions.verified_by` is NULL until a human confirms the terms.
    Applying an unconfirmed split silently corrupts an instrument's entire
    series in exactly the way this pipeline exists to prevent, so the default is
    to refuse. `allow_unverified=True` is the deliberate, explicit override.
    """


# -- ratio conventions ---------------------------------------------------------


def ratio_for(action_type: str, raw_terms: str) -> float:
    """Published terms (`'1:2'`) → the price multiplier stored as `ratio`.

    Kept beside the adjustment itself so `ratio` stays auditable against
    `raw_terms`: a wrong ratio is undetectable once the terms are discarded.
    """
    try:
        left_text, _, right_text = raw_terms.partition(":")
        left, right = float(left_text.strip()), float(right_text.strip())
    except (AttributeError, ValueError) as exc:
        raise AdjustmentError(f"cannot parse terms {raw_terms!r} as 'a:b'") from exc

    if left <= 0 or right <= 0:
        raise AdjustmentError(f"terms {raw_terms!r} must have positive parts on both sides")

    if action_type in ("split", "consolidation"):
        # 1:N -> 1/N shrinks the price; N:1 -> N raises it. Same arithmetic.
        return left / right
    if action_type == "bonus":
        # a free shares per b held: the holder ends with a+b where they had b.
        return right / (left + right)
    raise AdjustmentError(
        f"{action_type!r} has no ratio derivable from terms; "
        "dividends use dividend_ratio(amount, price)"
    )


def dividend_ratio(amount: float, price: float) -> float:
    """Dividend D at cum-price P → (P−D)/P.

    `price` is the close on the bar **before** the ex-date — the last price that
    still contained the dividend.
    """
    if price <= 0:
        raise AdjustmentError(f"dividend reference price must be positive, got {price!r}")
    if amount < 0:
        raise AdjustmentError(f"dividend amount cannot be negative, got {amount!r}")
    return (price - amount) / price


# -- factor construction -------------------------------------------------------


def _as_date(value: Any, field: str) -> dt.date:
    """Coerce whatever the database or a CSV handed us into a `date`."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError as exc:
            raise AdjustmentError(f"{field} {value!r} is not an ISO-8601 date") from exc
    raise AdjustmentError(f"{field} must be a date or ISO-8601 string, got {type(value).__name__}")


def _applicable(
    actions: Iterable[Mapping[str, Any]],
    method: str,
    allow_unverified: bool,
) -> list[tuple[dt.date, float]]:
    """Validate, filter by method, and reduce to `(ex_date, ratio)` pairs."""
    try:
        wanted = _ACTIONS_FOR_METHOD[method]
    except KeyError:
        raise AdjustmentError(
            f"unknown adjustment method {method!r}; expected one of {sorted(_ACTIONS_FOR_METHOD)}"
        ) from None

    pairs: list[tuple[dt.date, float]] = []
    for action in actions:
        action_type = action.get("action_type")
        if action_type not in wanted:
            continue

        ratio = action.get("ratio")
        if ratio is None or not isinstance(ratio, (int, float)) or float(ratio) <= 0:
            raise AdjustmentError(
                f"{action_type} on {action.get('instrument')} at {action.get('ex_date')} "
                f"has ratio {ratio!r}; a ratio must be a positive number"
            )

        if not action.get("verified_by") and not allow_unverified:
            raise UnverifiedActionError(
                f"{action_type} on {action.get('instrument')} at {action.get('ex_date')} is "
                "unverified (verified_by is NULL); unverified actions must not silently affect "
                "prices. Verify it, or pass allow_unverified=True to accept the risk explicitly."
            )

        pairs.append((_as_date(action.get("ex_date"), "ex_date"), float(ratio)))

    pairs.sort()
    return pairs


def cumulative_factors(
    dates: Sequence[Any],
    actions: Iterable[Mapping[str, Any]],
    method: str = "back_ratio_price",
    allow_unverified: bool = False,
) -> list[float]:
    """The back-adjustment factor for each bar date.

    A bar carries the product of every action whose ex-date is **strictly
    after** it. The ex-date bar itself is already post-action and so carries the
    factors of later actions only.

    Computed as a suffix product plus one binary search per bar rather than the
    obvious nested loop: a 25-year daily series against a few hundred actions is
    a hot path on every single load.
    """
    pairs = _applicable(actions, method, allow_unverified)
    if not pairs:
        return [1.0] * len(dates)

    ex_dates = [ex_date for ex_date, _ in pairs]
    # suffix[i] = product of ratios of actions i..end.
    suffix = [1.0] * (len(pairs) + 1)
    for i in range(len(pairs) - 1, -1, -1):
        suffix[i] = suffix[i + 1] * pairs[i][1]

    factors = []
    for value in dates:
        bar_date = _as_date(value, "bar date")
        # First action with ex_date > bar_date; everything from there applies.
        first = bisect.bisect_right(ex_dates, bar_date)
        factors.append(suffix[first])
    return factors


# -- the adjustment itself -----------------------------------------------------


def adjust(
    bars: pl.DataFrame,
    actions: Iterable[Mapping[str, Any]],
    method: str = "back_ratio_price",
    allow_unverified: bool = False,
) -> pl.DataFrame:
    """Back-adjust one instrument's OHLCV bars. Returns a new frame.

    `bars` covers a **single instrument** — the caller filters both the frame
    and `actions` (see `aqrl.data.SnapshotManager.load`, which groups by
    instrument). The returned frame gains an `adjustment_factor` column so the
    operation stays auditable: an unexplained result can always be traced back
    to the factor that produced it.
    """
    if "date" not in bars.columns:
        raise AdjustmentError(f"bars have no 'date' column (got: {', '.join(bars.columns) or 'none'})")

    factors = cumulative_factors(bars["date"].to_list(), actions, method, allow_unverified)
    factor_column = pl.Series("adjustment_factor", factors, dtype=pl.Float64)

    adjustments = [
        pl.col(column).cast(pl.Float64) * factor_column
        for column in PRICE_COLUMNS
        if column in bars.columns
    ]
    # Inverse on volume: a 1:2 split doubles the share count, so historical
    # volume must double for turnover to stay comparable across the event.
    adjustments += [
        pl.col(column).cast(pl.Float64) / factor_column
        for column in VOLUME_COLUMNS
        if column in bars.columns
    ]

    return bars.with_columns([*adjustments, factor_column])

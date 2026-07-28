"""Point-in-time universe resolution (TRD §14.3).

Without this, every equity backtest silently trades the companies that
survived. Testing "a NIFTY-50 momentum strategy" against *today's* NIFTY-50
back to 2000 does not test that strategy — it tests a portfolio selected with
25 years of hindsight, whose constituents were chosen partly *because* they did
well. The bias is large, one-directional, and invisible in the results.

**The half-open interval is the whole mechanism.** A constituent replaced on
date D belongs to the old universe up to D−1 and the new one from D:
`effective_from <= t < effective_to`. Never both, never neither — a
double-counted day is a phantom trade and a missing day is a phantom gap.

**Refusing is the feature.** `filter_bars` raises rather than passing bars
through when the membership table is empty for an index. A resolver that
silently degrades to "keep everything" is worse than no resolver at all: it
reintroduces exactly the bias it was built to remove, while looking like it
worked.

> ⚠️ **Still blocked on data (TRD §14.5).** The table, resolver, importer and
> tests all ship, but real snapshots stay `point_in_time_membership = 0` until
> price history exists for the ~100-150 stocks *ever* in NIFTY-50 — not today's
> 50. The ones that left are the invisible losses. Indian equity results must
> not reach live capital until that collection is done; index-level research is
> structurally unaffected and proceeds meanwhile.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

import polars as pl

from ..db.repositories import IndexMembershipRepository

__all__ = ["UniverseError", "UniverseResolver"]


class UniverseError(LookupError):
    """Point-in-time resolution was asked for and cannot be honestly provided."""


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


class UniverseResolver:
    """Answers *"which instruments were in this index on this date?"*"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.memberships = IndexMembershipRepository(conn)

    # -- resolution ------------------------------------------------------------

    def resolve(self, index_name: str, as_of: str | dt.date) -> set[str]:
        """The index's constituents on `as_of`, as a set."""
        return set(self.memberships.resolve(index_name, _iso(as_of)))

    def ever_members(self, index_name: str) -> set[str]:
        """Every instrument ever in the index — the price history actually needed.

        For NIFTY-50 over 2000-2025 this is ~100-150 tickers, not 50. Sizing
        data collection against `resolve(today)` instead of this is precisely
        how survivorship bias gets built into a dataset (TRD §14.3a).
        """
        return set(self.memberships.ever_members(index_name))

    # -- application -----------------------------------------------------------

    def intervals(self, index_name: str) -> dict[str, list[tuple[dt.date, dt.date | None]]]:
        """Per-instrument membership spans, `effective_to = None` meaning current."""
        spans: dict[str, list[tuple[dt.date, dt.date | None]]] = {}
        for row in self.memberships.find(index_name=index_name, order_by="instrument, effective_from"):
            start = _as_date(row["effective_from"])
            if start is None:
                continue
            spans.setdefault(row["instrument"], []).append((start, _as_date(row["effective_to"])))
        return spans

    def filter_bars(
        self,
        bars: pl.DataFrame,
        index_name: str,
        instrument_column: str = "instrument",
        date_column: str = "date",
    ) -> pl.DataFrame:
        """Drop every bar for an instrument that was not a member on that date.

        Raises when the index has no membership history: point-in-time
        resolution cannot be faked from an empty table, and quietly returning
        the input would hand back a survivorship-biased frame that *looks*
        resolved.
        """
        spans = self.intervals(index_name)
        if not spans:
            raise UniverseError(
                f"no index_membership rows for {index_name!r}: point-in-time resolution cannot be "
                "faked from an empty table. Import the membership history first "
                "(`aqrl membership import`), or the result carries survivorship bias."
            )
        for column in (instrument_column, date_column):
            if column not in bars.columns:
                raise UniverseError(f"bars have no {column!r} column; cannot resolve membership")

        # One OR-ed condition per instrument, evaluated vectorised rather than
        # row by row: a 25-year daily frame across 150 instruments is ~900k rows.
        conditions = [
            (pl.col(instrument_column) == instrument)
            & pl.any_horizontal(
                [
                    (pl.col(date_column) >= start)
                    & (pl.lit(True) if end is None else pl.col(date_column) < end)
                    for start, end in instrument_spans
                ]
            )
            for instrument, instrument_spans in spans.items()
        ]
        return bars.filter(pl.any_horizontal(conditions))

    # -- integrity -------------------------------------------------------------

    def check_overlaps(self, index_name: str) -> list[str]:
        """Instruments whose membership spans overlap — a data error, always.

        Two open intervals for one ticker means it is counted twice on every
        shared day, inflating its weight silently. Cheap to check at import and
        impossible to notice later.
        """
        problems: list[str] = []
        for instrument, spans in self.intervals(index_name).items():
            ordered = sorted(spans, key=lambda span: span[0])
            for (start, end), (next_start, _) in zip(ordered, ordered[1:], strict=False):
                if end is None or next_start < end:
                    problems.append(
                        f"{instrument} in {index_name}: span from {start} "
                        f"{'is open-ended' if end is None else f'ends {end}'} but another begins "
                        f"{next_start}; membership spans must not overlap"
                    )
        return problems


def _iso(value: str | dt.date) -> str:
    return value.isoformat() if isinstance(value, dt.date) else str(value)

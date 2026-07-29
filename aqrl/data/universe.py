"""Point-in-time universe resolution (TRD §14.3).

The universe is not "today's NIFTY-50 projected backwards" — it is *whichever
stocks were actually in the index on each bar's date*::

    for each bar date t:
        universe(t) = { instrument : effective_from <= t < effective_to }

**Why the half-open interval matters.** A constituent replaced on date D belongs
to the old universe up to D-1 and the new one from D — never both, never
neither. Closed intervals would double-count on every reconstitution date.

**Why this lives in the data layer.** Resolution happens on load, in code the
agent can read but never edit, so a strategy cannot quietly widen its own
universe by selecting from a static list.

**Status: machinery ready, data pending.** This requires price history for the
~100-150 stocks *ever* in NIFTY-50 over 2000-2025, not today's 50 — the
companies that left are exactly the ones whose losses are currently invisible,
and collecting only today's 50 reproduces the original bias with extra steps
(TRD §14.3a). Until that data exists, snapshots stay
`point_in_time_membership = 0` and Indian equity results must not reach live
capital. Index-level research is structurally unaffected and runs meanwhile.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

import polars as pl

from ..db.repositories import IndexMembershipRepository


class UniverseError(ValueError):
    """The point-in-time universe cannot be resolved."""


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:19]).date()


class UniverseResolver:
    """Resolves index membership as of a date, or across a whole frame."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repo = IndexMembershipRepository(conn)

    def resolve(self, index_name: str, as_of: str | date) -> frozenset[str]:
        """Which instruments were in the index on this date."""
        return frozenset(self.repo.resolve(index_name, _to_date(as_of).isoformat()))

    def ever_members(self, index_name: str) -> frozenset[str]:
        """Every instrument that was ever a member — the set price data is needed for."""
        return frozenset(self.repo.ever_members(index_name))

    def intervals(self, index_name: str) -> pl.DataFrame:
        """Membership intervals as a frame, for vectorised filtering."""
        rows = self.repo.find(index_name=index_name, order_by="instrument, effective_from")
        if not rows:
            return pl.DataFrame(
                schema={"instrument": pl.String, "effective_from": pl.Date, "effective_to": pl.Date}
            )
        return pl.DataFrame(
            {
                "instrument": [row["instrument"] for row in rows],
                "effective_from": [_to_date(row["effective_from"]) for row in rows],
                "effective_to": [
                    _to_date(row["effective_to"]) if row["effective_to"] else None for row in rows
                ],
            }
        )

    def check_overlaps(self, index_name: str) -> list[str]:
        """Report instruments whose membership intervals overlap.

        Overlap is a data error, not a market event: an instrument cannot join
        an index it has not left. Reported rather than raised, because the fix
        is a human correcting the membership record.
        """
        rows = self.repo.find(index_name=index_name, order_by="instrument, effective_from")
        problems: list[str] = []
        previous_instrument: str | None = None
        previous_end: date | None = None
        for row in rows:
            instrument = row["instrument"]
            start = _to_date(row["effective_from"])
            if instrument == previous_instrument and (previous_end is None or start < previous_end):
                problems.append(f"{instrument}: interval from {start} overlaps the previous one")
            previous_instrument = instrument
            previous_end = _to_date(row["effective_to"]) if row["effective_to"] else None
        return problems

    def filter_bars(
        self,
        bars: pl.DataFrame,
        index_name: str,
        *,
        date_column: str = "date",
        instrument_column: str = "instrument",
    ) -> pl.DataFrame:
        """Keep only bars whose instrument was an index member on that bar's date.

        This is the load-time enforcement: a strategy receives, per bar, the set
        of instruments that genuinely existed in the index at that moment.
        """
        for column in (date_column, instrument_column):
            if column not in bars.columns:
                raise UniverseError(f"bars have no {column!r} column (columns: {bars.columns})")

        members = self.intervals(index_name)
        if members.is_empty():
            raise UniverseError(
                f"no index_membership rows for {index_name!r}. Point-in-time resolution cannot be "
                "faked: without membership history every equity backtest carries survivorship bias "
                "(TRD §14.3). Import the membership record before loading this snapshot."
            )

        original_columns = bars.columns
        joined = bars.with_columns(pl.col(date_column).cast(pl.Date).alias("_pit_date")).join(
            members, left_on=instrument_column, right_on="instrument", how="inner"
        )
        kept = joined.filter(
            (pl.col("_pit_date") >= pl.col("effective_from"))
            & (pl.col("effective_to").is_null() | (pl.col("_pit_date") < pl.col("effective_to")))
        )
        # An instrument matches at most one interval unless the record overlaps,
        # which `check_overlaps` reports; dedupe so a data error degrades to a
        # correct universe rather than duplicated bars.
        return kept.select(original_columns).unique(
            subset=[date_column, instrument_column], keep="first", maintain_order=True
        )

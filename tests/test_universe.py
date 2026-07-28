"""Point-in-time universe resolution.

Without this, every equity backtest silently trades the companies that survived.
The half-open interval is the whole mechanism: a constituent replaced on date D
belongs to the old universe up to D-1 and the new one from D.
"""
from __future__ import annotations

import datetime as dt

import pytest

from aqrl.data.universe import UniverseError, UniverseResolver
from aqrl.db.repositories import IndexMembershipRepository

from .conftest import make_bars


@pytest.fixture
def membership(conn):
    """A miniature NIFTY-style index with one delisting and one merger."""
    repo = IndexMembershipRepository(conn)
    rows = [
        # Present throughout.
        ("NIFTY50", "RELIANCE", "2000-01-01", None, "periodic_review", None, None),
        # Joined after IPO.
        ("NIFTY50", "TCS", "2004-08-25", None, "ipo_inclusion", None, None),
        # Delisted — the invisible loss survivorship bias hides.
        ("NIFTY50", "SATYAM", "2000-01-01", "2009-01-12", "periodic_review", "delisting", None),
        # Merged away, with the successor recorded so the trail survives.
        ("NIFTY50", "IDEA", "2007-03-01", "2018-08-31", "replacement", "merger", "VODAFONEIDEA"),
    ]
    for index, instrument, start, end, added, removed, successor in rows:
        repo.insert(
            index_name=index,
            instrument=instrument,
            effective_from=start,
            effective_to=end,
            reason_added=added,
            reason_removed=removed,
            successor_instrument=successor,
        )
    return repo


def test_resolves_membership_as_of_a_date(conn, membership):
    resolver = UniverseResolver(conn)
    assert resolver.resolve("NIFTY50", "2008-06-01") == {"RELIANCE", "TCS", "SATYAM", "IDEA"}


def test_a_delisted_constituent_leaves_the_universe(conn, membership):
    resolver = UniverseResolver(conn)
    assert "SATYAM" in resolver.resolve("NIFTY50", "2009-01-11")
    assert "SATYAM" not in resolver.resolve("NIFTY50", "2009-01-12")


def test_the_interval_is_half_open(conn, membership):
    """`effective_from <= t < effective_to` — never both, never neither."""
    resolver = UniverseResolver(conn)
    assert "TCS" not in resolver.resolve("NIFTY50", "2004-08-24")
    assert "TCS" in resolver.resolve("NIFTY50", "2004-08-25")  # inclusive lower bound
    assert "IDEA" in resolver.resolve("NIFTY50", "2018-08-30")
    assert "IDEA" not in resolver.resolve("NIFTY50", "2018-08-31")  # exclusive upper bound


def test_null_effective_to_means_still_a_member(conn, membership):
    assert "RELIANCE" in UniverseResolver(conn).resolve("NIFTY50", "2025-01-01")


def test_ever_members_includes_those_that_left(conn, membership):
    """The union is what price history is needed for — not today's members."""
    ever = UniverseResolver(conn).ever_members("NIFTY50")
    assert ever == {"RELIANCE", "TCS", "SATYAM", "IDEA"}
    assert len(ever) > len(UniverseResolver(conn).resolve("NIFTY50", "2025-01-01"))


def test_merger_successor_is_recorded(conn, membership):
    row = membership.find(index_name="NIFTY50", instrument="IDEA")[0]
    assert row["successor_instrument"] == "VODAFONEIDEA"
    assert row["reason_removed"] == "merger"


def test_filter_bars_drops_non_members(conn, membership):
    bars = make_bars(
        {"RELIANCE": 1000.0, "SATYAM": 300.0}, start=dt.date(2009, 1, 5), days=20
    )
    filtered = UniverseResolver(conn).filter_bars(bars, "NIFTY50")

    satyam = filtered.filter(filtered["instrument"] == "SATYAM")
    assert satyam.height > 0, "SATYAM should exist before its delisting"
    assert max(satyam["date"].to_list()) < dt.date(2009, 1, 12)
    # RELIANCE was a member throughout, so nothing of its is dropped.
    assert filtered.filter(filtered["instrument"] == "RELIANCE").height == (
        bars.filter(bars["instrument"] == "RELIANCE").height
    )


def test_filter_bars_refuses_when_membership_is_absent(conn):
    """Point-in-time resolution cannot be faked from an empty table."""
    bars = make_bars({"X": 100.0}, days=10)
    with pytest.raises(UniverseError, match="no index_membership rows"):
        UniverseResolver(conn).filter_bars(bars, "NIFTY50")


def test_overlapping_intervals_are_reported(conn, membership):
    assert UniverseResolver(conn).check_overlaps("NIFTY50") == []
    membership.insert(
        index_name="NIFTY50",
        instrument="SATYAM",
        effective_from="2005-01-01",  # overlaps the 2000-2009 row
        effective_to=None,
    )
    problems = UniverseResolver(conn).check_overlaps("NIFTY50")
    assert len(problems) == 1 and "SATYAM" in problems[0]

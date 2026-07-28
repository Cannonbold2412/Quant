"""Snapshots — Stage 1's "done when": ingest, version, hash, load by ID.

The property worth stating plainly, because the whole design turns on it:
**adding a corporate action must change `corporate_actions_version` and leave
`raw_content_hash` untouched.** Back-adjusting files in place would instead
change every hash and mark the entire archive incomparable — one split
invalidating years of results (TRD §14.2a).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from aqrl.data import SnapshotError, SnapshotManager
from aqrl.db.repositories import CorporateActionRepository, SnapshotRepository, ValidationFlagRepository

from .conftest import make_bars


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """RELIANCE with a recorded split; TCS with an unrecorded 35% drop."""
    path = tmp_path / "src" / "bars.parquet"
    path.parent.mkdir(parents=True)
    make_bars(
        {"RELIANCE": 1000.0, "TCS": 2000.0},
        days=300,
        events={"RELIANCE": (dt.date(2020, 3, 2), 0.5), "TCS": (dt.date(2020, 6, 1), 0.65)},
    ).write_parquet(path)
    return path


@pytest.fixture
def manager(conn, settings, loader) -> SnapshotManager:
    return SnapshotManager(conn, settings=settings, loader=loader)


@pytest.fixture
def split_recorded(conn):
    CorporateActionRepository(conn).insert(
        instrument="RELIANCE",
        market="nse_equity",
        action_type="split",
        ex_date="2020-03-02",
        ratio=0.5,
        raw_terms="1:2",
        source="NSE filing",
        verified_by="human",
    )


def _ingest(manager: SnapshotManager, source: Path) -> int:
    return manager.ingest(source, "nse_equity", "daily", "cash_equity")


def test_ingest_records_identity_and_counts(manager, source, conn, split_recorded):
    snapshot_id = _ingest(manager, source)
    snapshot = SnapshotRepository(conn).get(snapshot_id)
    assert snapshot["instrument_count"] == 2
    assert snapshot["bar_count"] > 0
    assert len(snapshot["raw_content_hash"]) == 64
    assert len(snapshot["corporate_actions_version"]) == 64
    assert snapshot["adjusted"] == 1


def test_reingesting_identical_bytes_returns_the_same_snapshot(manager, source, split_recorded):
    assert _ingest(manager, source) == _ingest(manager, source)


def test_a_new_corporate_action_changes_only_the_actions_version(
    manager, source, conn, split_recorded
):
    """The load-bearing property: one split must not invalidate the archive."""
    first_id = _ingest(manager, source)
    first = SnapshotRepository(conn).get(first_id)

    CorporateActionRepository(conn).insert(
        instrument="TCS",
        market="nse_equity",
        action_type="bonus",
        ex_date="2020-06-01",
        ratio=0.65,
        raw_terms="35:65",
        source="NSE filing",
        verified_by="human",
    )
    second_id = _ingest(manager, source)
    second = SnapshotRepository(conn).get(second_id)

    assert second_id != first_id
    assert second["raw_content_hash"] == first["raw_content_hash"]
    assert second["corporate_actions_version"] != first["corporate_actions_version"]


def test_the_unrecorded_drop_is_flagged(manager, source, conn, split_recorded):
    snapshot_id = _ingest(manager, source)
    flags = ValidationFlagRepository(conn).pending(snapshot_id)
    assert [f["instrument"] for f in flags] == ["TCS"], "only the unrecorded event should flag"
    assert flags[0]["flag_type"] == "unexplained_jump"


def test_pending_flags_block_loading(manager, source, split_recorded):
    """The scheduler will not dispatch experiments against an unvalidated snapshot."""
    snapshot_id = _ingest(manager, source)
    with pytest.raises(ValueError, match="unresolved validation flag"):
        manager.load(snapshot_id)


def test_resolving_flags_makes_the_snapshot_loadable(manager, source, conn, split_recorded):
    snapshot_id = _ingest(manager, source)
    flags = ValidationFlagRepository(conn)
    for flag in flags.pending(snapshot_id):
        flags.resolve(flag["id"], "genuine_move", "tester")
    assert manager.revalidate(snapshot_id) == "valid"
    assert manager.load(snapshot_id).height > 0


def test_a_resolved_data_error_marks_the_snapshot_invalid(manager, source, conn, split_recorded):
    snapshot_id = _ingest(manager, source)
    flags = ValidationFlagRepository(conn)
    for flag in flags.pending(snapshot_id):
        flags.resolve(flag["id"], "data_error", "tester")
    assert manager.revalidate(snapshot_id) == "invalid"
    with pytest.raises(ValueError):
        manager.load(snapshot_id)


def test_pending_is_not_an_accepted_resolution(conn):
    """A flag cannot be waved through by writing its current state back."""
    with pytest.raises(ValueError, match="resolution must be one of"):
        ValidationFlagRepository(conn).resolve(1, "pending", "tester")


def test_flags_require_a_named_resolver(conn):
    with pytest.raises(ValueError, match="resolved_by is required"):
        ValidationFlagRepository(conn).resolve(1, "genuine_move", "")


def _validated(manager, source, conn) -> int:
    snapshot_id = _ingest(manager, source)
    flags = ValidationFlagRepository(conn)
    for flag in flags.pending(snapshot_id):
        flags.resolve(flag["id"], "genuine_move", "tester")
    manager.revalidate(snapshot_id)
    return snapshot_id


def test_load_applies_adjustment(manager, source, conn, split_recorded):
    snapshot_id = _validated(manager, source, conn)
    adjusted = manager.load(snapshot_id, instrument="RELIANCE")
    returns = [r for r in adjusted["close"].pct_change().to_list() if r is not None]
    assert min(returns) > -0.01, "the phantom split gap survived adjustment"

    raw = manager.load(snapshot_id, instrument="RELIANCE", adjusted=False)
    raw_returns = [r for r in raw["close"].pct_change().to_list() if r is not None]
    assert min(raw_returns) < -0.49, "raw prices should still show the print"


def test_load_filters_by_instrument_and_date(manager, source, conn, split_recorded):
    snapshot_id = _validated(manager, source, conn)
    frame = manager.load(
        snapshot_id, instrument="TCS", start="2020-02-01", end="2020-02-29"
    )
    assert set(frame["instrument"].unique().to_list()) == {"TCS"}
    assert min(frame["date"].to_list()) >= dt.date(2020, 2, 1)
    assert max(frame["date"].to_list()) <= dt.date(2020, 2, 29)


def test_load_rejects_an_unknown_instrument(manager, source, conn, split_recorded):
    snapshot_id = _validated(manager, source, conn)
    with pytest.raises(SnapshotError, match="not in snapshot"):
        manager.load(snapshot_id, instrument="NOT_LISTED")


def test_vaulted_snapshots_have_no_read_path(manager, source, conn, split_recorded, settings):
    """TRD §15.2: not "should not" — cannot."""
    snapshot_id = manager.ingest(
        source, "nse_equity", "daily", "cash_equity", in_vault=True
    )
    flags = ValidationFlagRepository(conn)
    for flag in flags.pending(snapshot_id):
        flags.resolve(flag["id"], "genuine_move", "tester")
    manager.revalidate(snapshot_id)
    with pytest.raises(PermissionError, match="vault"):
        manager.load(snapshot_id)


def test_point_in_time_is_false_without_membership_data(manager, source, conn, split_recorded):
    """Blocked on data collection, and honest about it (TRD §14.5)."""
    snapshot_id = _ingest(manager, source)
    snapshot = SnapshotRepository(conn).get(snapshot_id)
    assert snapshot["point_in_time_membership"] == 0
    assert snapshot["survivorship_handled"] == 0
    assert "survivorship" in snapshot["validation_report"]


def test_raw_files_are_copied_not_moved(manager, source, split_recorded, settings):
    _ingest(manager, source)
    assert source.exists(), "the source file must not be consumed"
    stored = list((settings.data_root / "snapshots").rglob("*.parquet"))
    assert len(stored) == 1


def test_stored_path_is_relative_to_the_data_root(manager, source, conn, split_recorded, settings):
    """Backend-Schema §16: local -> object storage is a config change."""
    snapshot_id = _ingest(manager, source)
    stored = SnapshotRepository(conn).get(snapshot_id)["storage_path"]
    assert not Path(stored).is_absolute()
    assert settings.from_data_relative(stored).exists()


def test_missing_required_columns_are_reported(manager, tmp_path: Path):
    path = tmp_path / "bad.parquet"
    pl.DataFrame({"date": [dt.date(2020, 1, 1)], "close": [1.0]}).write_parquet(path)
    with pytest.raises(SnapshotError, match="missing required column"):
        manager.ingest(path, "nse_equity", "daily", "cash_equity")


def test_unknown_asset_class_is_rejected_before_any_copy(manager, source, settings):
    with pytest.raises(LookupError):
        manager.ingest(source, "nse_equity", "daily", "perpetual")
    assert not (settings.data_root / "snapshots").exists()

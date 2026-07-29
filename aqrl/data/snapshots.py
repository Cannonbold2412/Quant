"""Immutable, content-addressed market-data snapshots (TRD §13.1, §14.2a).

An experiment references a snapshot ID, never "whatever was on disk that day".

**Identity is the pair `(raw_content_hash, corporate_actions_version)`.** This
is the design's load-bearing detail. Back-adjusting files in place would rewrite
all historical prices, so a single new split would change every content hash and
mark the entire archive incomparable. Keeping actions in their own versioned
table means adjustment applies at load time and only the actions version
changes — comparability is scoped to what actually changed.

Raw files are copied into a content-addressed store and **never rewritten**.
"""
from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from ..config import Settings, get_settings
from ..hashing import hash_files
from ..logging import get_logger, log_context
from ..profiles import ProfileLoader
from ..db.repositories import (
    CorporateActionRepository,
    IndexMembershipRepository,
    SnapshotRepository,
    ValidationFlagRepository,
)
from .adjustment import AdjustmentMethod, adjust
from .universe import UniverseResolver
from .validators import ValidationContext, run_validators

log = get_logger(__name__)

REQUIRED_COLUMNS = ("date", "open", "high", "low", "close")
OPTIONAL_COLUMNS = ("volume", "instrument")


class SnapshotError(ValueError):
    """A snapshot cannot be ingested or loaded as specified."""


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:19]).date()


def _parquet_files(source: Path) -> list[Path]:
    if source.is_file():
        if source.suffix != ".parquet":
            raise SnapshotError(f"{source} is not a Parquet file")
        return [source]
    files = sorted(source.rglob("*.parquet"))
    if not files:
        raise SnapshotError(f"no .parquet files under {source}")
    return files


def _validate_columns(frame: pl.DataFrame, where: str) -> None:
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise SnapshotError(
            f"{where} is missing required column(s) {missing}. "
            f"Expected {list(REQUIRED_COLUMNS)} plus optionally {list(OPTIONAL_COLUMNS)}; "
            f"found {frame.columns}."
        )


class SnapshotManager:
    """Ingests, validates and loads market-data snapshots."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings | None = None,
        loader: ProfileLoader | None = None,
    ) -> None:
        self.conn = conn
        self.settings = settings or get_settings()
        self.loader = loader or ProfileLoader(self.settings.profiles_dir)
        self.snapshots = SnapshotRepository(conn)
        self.actions = CorporateActionRepository(conn)
        self.flags = ValidationFlagRepository(conn)
        self.membership = IndexMembershipRepository(conn)

    # -- ingest ----------------------------------------------------------------

    def ingest(
        self,
        source: Path | str,
        market: str,
        timeframe: str,
        asset_class: str,
        *,
        adjustment_method: AdjustmentMethod = "back_ratio_price",
        in_vault: bool = False,
        validation_note: str | None = None,
        validator_options: dict[str, Any] | None = None,
    ) -> int:
        """Ingest raw Parquet into an immutable, hashed snapshot.

        Returns the snapshot id. Re-ingesting identical bytes against an
        unchanged actions table returns the existing snapshot rather than
        creating a second identity for the same data.
        """
        source = Path(source).resolve()
        market_profile = self.loader.load_market(market)
        # Resolving the full triple validates that the asset class and cost
        # model exist before anything is copied to disk.
        self.loader.resolve(market, timeframe, asset_class)

        files = _parquet_files(source)
        root = source if source.is_dir() else source.parent
        frame = pl.read_parquet(files)
        _validate_columns(frame, str(source))

        instruments = (
            sorted(frame["instrument"].unique().to_list()) if "instrument" in frame.columns else []
        )
        dates = [_to_date(value) for value in frame["date"].to_list()]
        raw_content_hash = hash_files(files, root)
        actions_version = self.actions.version_for(instruments, market)

        existing = self.snapshots.by_identity(raw_content_hash, actions_version)
        if existing is not None:
            log.info(
                "snapshot already ingested",
                extra={"snapshot_id": existing["id"], "raw_content_hash": raw_content_hash[:12]},
            )
            return int(existing["id"])

        # Point-in-time resolution requires both a declared index and an actual
        # membership record. Absent either, the flag stays false and the
        # snapshot is barred from live capital (TRD §14.5).
        index_name = market_profile.universe.index_name
        has_membership = bool(index_name and self.membership.ever_members(index_name))
        point_in_time = bool(market_profile.universe.point_in_time_available and has_membership)

        storage_path = (
            self.settings.data_root
            / "snapshots"
            / market
            / timeframe
            / raw_content_hash[:2]
            / raw_content_hash
        )
        storage_path.mkdir(parents=True, exist_ok=True)
        for file in files:
            destination = storage_path / file.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():  # raw files are never rewritten
                shutil.copy2(file, destination)

        report: dict[str, Any] = {
            "source": str(source),
            "files": len(files),
            "adjustment_method": adjustment_method,
            "point_in_time_resolved": point_in_time,
        }
        if validation_note:
            report["note"] = validation_note
        if not point_in_time and index_name:
            report["survivorship"] = (
                f"point-in-time membership unavailable for {index_name}; results carry survivorship "
                "bias and must not reach live capital (TRD §14.5)"
            )

        snapshot_id = self.snapshots.insert(
            market=market,
            timeframe=timeframe,
            asset_class=asset_class,
            period_start=min(dates).isoformat(),
            period_end=max(dates).isoformat(),
            instrument_count=len(instruments) or 1,
            bar_count=frame.height,
            storage_path=self.settings.to_data_relative(storage_path),
            raw_content_hash=raw_content_hash,
            corporate_actions_version=actions_version,
            adjustment_method=adjustment_method,
            adjusted=adjustment_method != "none",
            point_in_time_membership=point_in_time,
            survivorship_handled=point_in_time,
            in_vault=in_vault,
            validation_status="pending",
            validation_report=report,
        )

        with log_context(snapshot_id=snapshot_id):
            self._run_validation(
                snapshot_id, frame, market_profile, instruments, validator_options or {}
            )
            status = self.snapshots.refresh_validation_status(snapshot_id)
            log.info(
                "snapshot ingested",
                extra={
                    "market": market,
                    "timeframe": timeframe,
                    "bars": frame.height,
                    "instruments": len(instruments) or 1,
                    "validation_status": status,
                },
            )
        return snapshot_id

    def _run_validation(
        self,
        snapshot_id: int,
        frame: pl.DataFrame,
        market_profile: Any,
        instruments: Sequence[str],
        options: dict[str, Any],
    ) -> None:
        """Run validators on the RAW frame and record every flag."""
        actions: list[dict[str, Any]] = []
        for instrument in instruments or []:
            # verified_only=False on purpose: an unverified action still
            # explains a jump, and treating it as unknown would bury the real
            # missing-action cases in false alarms.
            actions.extend(
                self.actions.for_instrument(instrument, market_profile.name, verified_only=False)
            )

        if market_profile.universe.index_name and "index_size" not in options:
            declared = len(market_profile.universe.instruments)
            if declared:
                options = {**options, "index_size": declared}

        context = ValidationContext(
            bars=frame, market=market_profile, actions=actions, options=options
        )
        for flag in run_validators(context):
            self.flags.raise_flag(
                snapshot_id=snapshot_id,
                flag_type=flag.flag_type,
                instrument=flag.instrument,
                bar_date=flag.bar_date,
                observed_value=flag.observed_value,
                threshold=flag.threshold,
                detail=flag.detail,
            )

    def revalidate(self, snapshot_id: int) -> str:
        """Recompute validation status after flags have been resolved."""
        return self.snapshots.refresh_validation_status(snapshot_id)

    # -- load ------------------------------------------------------------------

    def load(
        self,
        snapshot_id: int,
        instrument: str | None = None,
        start: str | date | None = None,
        end: str | date | None = None,
        *,
        adjusted: bool = True,
        point_in_time: bool = True,
    ) -> pl.DataFrame:
        """Load a snapshot by ID: raw Parquet, adjusted, universe-resolved.

        Refuses any snapshot that is not `valid` or that sits in the vault.
        `adjusted=False` exists for the validators and for auditing what the
        exchange actually printed — never for research.
        """
        snapshot = self.snapshots.assert_loadable(snapshot_id)
        storage_path = self.settings.from_data_relative(snapshot["storage_path"])
        if not storage_path.exists():
            raise SnapshotError(
                f"snapshot {snapshot_id} storage is missing at {storage_path}. "
                "Snapshots are immutable; re-ingest from source rather than editing in place."
            )

        frame = pl.read_parquet(sorted(storage_path.rglob("*.parquet")))
        _validate_columns(frame, f"snapshot {snapshot_id}")
        frame = frame.with_columns(pl.col("date").cast(pl.Date))

        if instrument is not None:
            if "instrument" not in frame.columns:
                raise SnapshotError(f"snapshot {snapshot_id} is single-instrument; drop the filter")
            frame = frame.filter(pl.col("instrument") == instrument)
            if frame.is_empty():
                raise SnapshotError(f"instrument {instrument!r} is not in snapshot {snapshot_id}")
        if start is not None:
            frame = frame.filter(pl.col("date") >= _to_date(start))
        if end is not None:
            frame = frame.filter(pl.col("date") <= _to_date(end))

        if adjusted and snapshot["adjustment_method"] != "none":
            frame = self._adjust(frame, snapshot)

        if point_in_time and snapshot["point_in_time_membership"]:
            index_name = self.loader.load_market(snapshot["market"]).universe.index_name
            if index_name and "instrument" in frame.columns:
                frame = UniverseResolver(self.conn).filter_bars(frame, index_name)

        return frame.sort([c for c in ("instrument", "date") if c in frame.columns])

    def _adjust(self, frame: pl.DataFrame, snapshot: dict[str, Any]) -> pl.DataFrame:
        """Apply back-adjustment per instrument, using verified actions only."""
        market = snapshot["market"]
        method: AdjustmentMethod = snapshot["adjustment_method"]

        if "instrument" not in frame.columns:
            return adjust(frame, [], method)

        adjusted_parts = []
        for (instrument,), group in frame.group_by(["instrument"], maintain_order=True):
            actions = self.actions.for_instrument(str(instrument), market, verified_only=True)
            adjusted_parts.append(adjust(group, actions, method))
        return pl.concat(adjusted_parts, how="vertical")

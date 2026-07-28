"""The data layer — immutable raw snapshots, adjusted only on the way out.

The one property the whole design turns on (TRD §14.2a):

> **Adding a corporate action changes `corporate_actions_version` and leaves
> `raw_content_hash` untouched.**

Back-adjusting the stored Parquet in place would instead change every hash the
moment a new split is filed, marking the entire archive incomparable — one
corporate action invalidating years of results. So a snapshot's identity is the
**pair**, raw bytes are never rewritten, and adjustment happens at load time on
every read.

The pieces:

* `SnapshotManager` — ingest, version, hash, load by ID. Stage 1's "done when".
* `adjustment` — back-ratio corporate-action adjustment (TRD §14.2).
* `universe` — point-in-time index membership (TRD §14.3).
* `validators` — ingest-time checks for what adjustment cannot catch (TRD §14.4).
"""
from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import polars as pl

from ..config import Settings, get_settings
from ..db.repositories import (
    CorporateActionRepository,
    SnapshotRepository,
    ValidationFlagRepository,
)
from ..hashing import hash_file, hash_files
from ..logging import get_logger
from ..profiles import ProfileLoader
from .adjustment import AdjustmentError, adjust
from .universe import UniverseError, UniverseResolver
from .validators import ValidationContext, ValidationFlag, run_validators

__all__ = [
    "AdjustmentError",
    "SnapshotError",
    "SnapshotManager",
    "UniverseError",
    "UniverseResolver",
    "adjust",
    "run_validators",
]

logger = get_logger(__name__)

# The OHLCV contract every snapshot satisfies. Enforced at ingest so a missing
# column is one clear error at import rather than a KeyError inside a backtest
# three stages later.
REQUIRED_COLUMNS = ("date", "instrument", "open", "high", "low", "close", "volume")

SNAPSHOT_DIRNAME = "snapshots"


class SnapshotError(ValueError):
    """A snapshot cannot be ingested or loaded as asked."""


class SnapshotManager:
    """Content-addressed market-data snapshots.

    An experiment references a snapshot ID, never *"whatever was on disk that
    day"* (TRD §13.1). Ingest is idempotent on identity, so re-running it is
    always safe and never duplicates storage.
    """

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

    # -- ingest ----------------------------------------------------------------

    def ingest(
        self,
        path: Path | str,
        market: str,
        timeframe: str,
        asset_class: str,
        adjustment_method: str = "back_ratio_price",
        in_vault: bool = False,
        validation_note: str | None = None,
    ) -> int:
        """Ingest raw Parquet into an immutable, hashed snapshot.

        Order matters. The profile resolves and the columns validate **before**
        anything is copied, so a rejected ingest leaves no partial state on
        disk. Validators then run on the **raw** frame — the only stage at which
        a missing corporate action is still visible.
        """
        source = Path(path)
        if not source.exists():
            raise SnapshotError(f"no such path: {source}")

        # Raises ProfileError (a LookupError) for an asset class this market does
        # not declare. Deliberately first: no disk is touched until it passes.
        resolved = self.loader.resolve(market, timeframe, asset_class)

        frame = self._read(source)
        missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            raise SnapshotError(
                f"missing required column(s) {', '.join(missing)} in {source.name} "
                f"(found: {', '.join(frame.columns)})"
            )

        instruments = sorted(str(value) for value in frame["instrument"].unique().to_list())
        raw_content_hash = self._content_hash(source)
        actions_version = self.actions.version_for(instruments, market)

        existing = self.snapshots.by_identity(raw_content_hash, actions_version)
        if existing is not None:
            # Identity is the pair. Same bytes AND same actions history means the
            # same snapshot — re-ingesting must not fork the archive.
            logger.info(
                "snapshot already ingested",
                extra={"snapshot_id": existing["id"], "raw_content_hash": raw_content_hash},
            )
            return int(existing["id"])

        stored = self._store(source, raw_content_hash)
        dates = frame["date"].to_list()

        snapshot_id = self.snapshots.insert(
            market=market,
            timeframe=timeframe,
            asset_class=asset_class,
            period_start=_iso(min(dates)),
            period_end=_iso(max(dates)),
            instrument_count=len(instruments),
            bar_count=frame.height,
            storage_path=self.settings.to_data_relative(stored),
            raw_content_hash=raw_content_hash,
            corporate_actions_version=actions_version,
            adjustment_method=adjustment_method,
            adjusted=adjustment_method != "none",
            point_in_time_membership=False,
            survivorship_handled=False,
            in_vault=in_vault,
        )

        flags = self._validate(frame, market, resolved, snapshot_id)
        self._stamp_report(snapshot_id, resolved, adjustment_method, flags, validation_note)
        self.snapshots.refresh_validation_status(snapshot_id)

        logger.info(
            "snapshot ingested",
            extra={
                "snapshot_id": snapshot_id,
                "bar_count": frame.height,
                "instrument_count": len(instruments),
                "flag_count": len(flags),
            },
        )
        return snapshot_id

    def revalidate(self, snapshot_id: int) -> str:
        """Recompute validation status after a human resolved flags."""
        return self.snapshots.refresh_validation_status(snapshot_id)

    # -- load ------------------------------------------------------------------

    def load(
        self,
        snapshot_id: int,
        instrument: str | None = None,
        start: str | dt.date | None = None,
        end: str | dt.date | None = None,
        adjusted: bool = True,
    ) -> pl.DataFrame:
        """Load a snapshot by ID, back-adjusted unless asked otherwise.

        `assert_loadable` refuses anything not cleared for experiments — pending
        flags, a resolved data error, or a vaulted snapshot the research loop
        has no read path to (TRD §15.2). That check is the reason a corrupted
        series cannot quietly become a research finding.
        """
        snapshot = self.snapshots.assert_loadable(snapshot_id)
        frame = self._read(self.settings.from_data_relative(snapshot["storage_path"]))

        if instrument is not None:
            available = set(frame["instrument"].unique().to_list())
            if instrument not in available:
                raise SnapshotError(
                    f"{instrument!r} is not in snapshot {snapshot_id} "
                    f"({len(available)} instrument(s) available)"
                )
            frame = frame.filter(pl.col("instrument") == instrument)

        if start is not None:
            frame = frame.filter(pl.col("date") >= _as_date(start))
        if end is not None:
            frame = frame.filter(pl.col("date") <= _as_date(end))

        method = snapshot["adjustment_method"]
        if not adjusted or method == "none" or frame.height == 0:
            return frame.sort(["date", "instrument"])

        return self._adjust_per_instrument(frame, snapshot["market"], method)

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _read(path: Path) -> pl.DataFrame:
        try:
            return pl.read_parquet(path)
        except Exception as exc:  # a malformed file should name itself
            raise SnapshotError(f"cannot read Parquet at {path}: {exc}") from exc

    @staticmethod
    def _content_hash(source: Path) -> str:
        """sha256 over the raw bytes — the archive's stable identity."""
        if source.is_dir():
            return hash_files(sorted(source.rglob("*.parquet")), root=source)
        return hash_file(source)

    def _store(self, source: Path, raw_content_hash: str) -> Path:
        """Copy (never move) into the content-addressed store.

        The source is somebody's download directory; consuming it would make an
        ingest unrepeatable. Fanning out on the hash prefix keeps any single
        directory small once the archive holds thousands of snapshots.
        """
        target = (
            self.settings.data_root
            / SNAPSHOT_DIRNAME
            / raw_content_hash[:2]
            / f"{raw_content_hash}.parquet"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
        return target

    def _validate(
        self,
        frame: pl.DataFrame,
        market: str,
        resolved: Any,
        snapshot_id: int,
    ) -> list[ValidationFlag]:
        """Run the validators on RAW prices and persist every question raised.

        Unverified actions are included here on purpose: a filed-but-unchecked
        split still *explains* a jump even though it is refused the power to
        move prices. Verification gates the adjustment, not the explanation.
        """
        instruments = sorted(str(value) for value in frame["instrument"].unique().to_list())
        actions = [
            action
            for instrument in instruments
            for action in self.actions.for_instrument(instrument, market, verified_only=False)
        ]
        options: dict[str, Any] = {}
        declared = len(resolved.market.universe.instruments)
        if resolved.market.universe.index_name and declared:
            options["index_size"] = declared

        flags = run_validators(
            ValidationContext(bars=frame, market=resolved.market, actions=actions, options=options)
        )
        for flag in flags:
            self.flags.raise_flag(snapshot_id, **flag.as_row())
        return flags

    def _stamp_report(
        self,
        snapshot_id: int,
        resolved: Any,
        adjustment_method: str,
        flags: list[ValidationFlag],
        note: str | None,
    ) -> None:
        """Record what was checked, what was found, and what remains unfixed.

        `survivorship` is always present, even — especially — when unhandled. A
        snapshot that silently omits the caveat is how a known-biased dataset
        gets treated as a clean one six months later (TRD §14.5).
        """
        counts: dict[str, int] = {}
        for flag in flags:
            counts[flag.flag_type] = counts.get(flag.flag_type, 0) + 1

        universe = resolved.market.universe
        report: dict[str, Any] = {
            "adjustment": {
                "method": adjustment_method,
                "applied_at": "load_time",
                "raw_rewritten": False,
            },
            "survivorship": {
                "handled": False,
                "point_in_time_available": universe.point_in_time_available,
                "index_name": universe.index_name,
                "note": (
                    "Point-in-time membership requires price history for every instrument EVER in "
                    f"{universe.index_name or 'the index'} — ~100-150 for NIFTY-50, not today's 50. "
                    "That collection is still open, so results from this snapshot carry "
                    "survivorship bias and must not reach live capital (TRD §14.5)."
                ),
            },
            "flags": counts,
            "flags_total": len(flags),
        }
        if note:
            report["note"] = note
        self.snapshots.update(snapshot_id, validation_report=report)

    def _adjust_per_instrument(
        self, frame: pl.DataFrame, market: str, method: str
    ) -> pl.DataFrame:
        """Adjust each instrument against its own verified actions.

        `verified_only=True`: an unverified action must not silently move
        prices, so it never reaches `adjust()` and the refusal there stays a
        genuine backstop rather than a path that is routinely overridden.
        """
        adjusted_groups = [
            adjust(group, self.actions.for_instrument(str(name[0]), market, True), method)
            for name, group in frame.sort(["instrument", "date"]).group_by(
                ["instrument"], maintain_order=True
            )
        ]
        return pl.concat(adjusted_groups).sort(["date", "instrument"])


def _as_date(value: str | dt.date) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise SnapshotError(f"{value!r} is not an ISO-8601 date") from exc


def _iso(value: Any) -> str:
    return _as_date(value).isoformat()

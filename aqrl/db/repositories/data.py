"""Repositories for the data-integrity tables (Backend-Schema §12, TRD §14).

The rule these enforce, and the reason they are not just generic CRUD:

    A snapshot with pending validation flags cannot be marked valid.

Every flag is either a real market event or a data error, and a human must say
which (TRD §14.4). `SnapshotRepository.refresh_validation_status` is the single
place that decision is applied, so no caller can route around it.
"""
from __future__ import annotations

from typing import Any

from .base import Repository, Row, utcnow_iso


class SnapshotRepository(Repository):
    table = "data_snapshots"
    json_columns = frozenset({"validation_report"})

    def by_identity(self, raw_content_hash: str, corporate_actions_version: str) -> Row | None:
        """Snapshot identity is the pair, not the price hash alone (TRD §14.2a).

        A new split bumps the actions version and leaves the price hash
        untouched, so one corporate action does not invalidate the archive.
        """
        return self._decode(
            self.conn.execute(
                "SELECT * FROM data_snapshots WHERE raw_content_hash = ? AND corporate_actions_version = ?",
                (raw_content_hash, corporate_actions_version),
            ).fetchone()
        )

    def pending_flag_count(self, snapshot_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM data_validation_flags WHERE snapshot_id = ? AND resolution = 'pending'",
            (snapshot_id,),
        ).fetchone()
        return int(row["n"])

    def refresh_validation_status(self, snapshot_id: int) -> str:
        """Recompute and store validation status. The only writer of that field.

        `invalid` is reserved for a resolved `data_error`: a snapshot a human has
        looked at and rejected. `pending` means nobody has looked yet — which is
        why the scheduler treats it as unusable rather than merely unverified.
        """
        if self.pending_flag_count(snapshot_id):
            status = "pending"
        else:
            errors = self.conn.execute(
                "SELECT COUNT(*) AS n FROM data_validation_flags "
                "WHERE snapshot_id = ? AND resolution = 'data_error'",
                (snapshot_id,),
            ).fetchone()["n"]
            status = "invalid" if errors else "valid"
        self.update(snapshot_id, validation_status=status)
        return status

    def latest_usable(self, market: str, timeframe: str, asset_class: str) -> Row | None:
        """The newest valid, non-vaulted snapshot for a (market, timeframe,
        asset_class) — what Stage 11's replay backtests against. `valid` and
        `in_vault = 0` mirror `assert_loadable`'s two gates; `period_end DESC`
        picks the snapshot with the most forward bars to replay."""
        return self._decode(
            self.conn.execute(
                """SELECT * FROM data_snapshots
                    WHERE market = ? AND timeframe = ? AND asset_class = ?
                      AND validation_status = 'valid' AND in_vault = 0
                    ORDER BY period_end DESC, id DESC LIMIT 1""",
                (market, timeframe, asset_class),
            ).fetchone()
        )

    def assert_loadable(self, snapshot_id: int) -> Row:
        """Fetch a snapshot, refusing anything not cleared for experiments.

        Backend-Schema §15 Q12 — *"is this snapshot safe to run experiments
        against?"* Answering it at load time is what stops a corrupted series
        quietly becoming a research finding.
        """
        snapshot = self.get(snapshot_id)
        if snapshot is None:
            raise KeyError(f"no such snapshot: {snapshot_id}")
        if snapshot["validation_status"] != "valid":
            pending = self.pending_flag_count(snapshot_id)
            raise ValueError(
                f"snapshot {snapshot_id} is {snapshot['validation_status']} "
                f"({pending} unresolved validation flag(s)); a human must resolve each before it can be loaded"
            )
        if snapshot["in_vault"]:
            raise PermissionError(
                f"snapshot {snapshot_id} is in the vault; the research loop has no read path to it (TRD §15.2)"
            )
        return snapshot


class CorporateActionRepository(Repository):
    table = "corporate_actions"

    def for_instrument(self, instrument: str, market: str, verified_only: bool = True) -> list[Row]:
        """Actions in ex-date order, oldest first.

        `verified_only` defaults to True because an unverified action must not
        silently move prices (Backend-Schema §12). The adjustment pipeline uses
        the default; the validators deliberately do not, since an unverified
        record still explains a jump.
        """
        sql = "SELECT * FROM corporate_actions WHERE instrument = ? AND market = ?"
        if verified_only:
            sql += " AND verified_by IS NOT NULL"
        sql += " ORDER BY ex_date, id"
        rows = self.conn.execute(sql, (instrument, market)).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[misc]

    def version_for(self, instruments: list[str], market: str) -> str:
        """Content hash of every action affecting these instruments.

        This is `data_snapshots.corporate_actions_version`. It covers verified
        *and* unverified rows: adding an unverified action changes what the
        snapshot resolves against even before a human confirms it, and pretending
        otherwise would make two different states share one identity.
        """
        from ...hashing import content_hash

        if not instruments:
            return content_hash([])
        placeholders = ", ".join("?" for _ in instruments)
        rows = self.conn.execute(
            f"""SELECT instrument, action_type, ex_date, ratio, raw_terms, verified_by
                  FROM corporate_actions
                 WHERE market = ? AND instrument IN ({placeholders})
                 ORDER BY instrument, ex_date, action_type, ratio""",
            [market, *instruments],
        ).fetchall()
        return content_hash([dict(row) for row in rows])


class IndexMembershipRepository(Repository):
    table = "index_membership"

    def resolve(self, index_name: str, as_of: str) -> list[str]:
        """The point-in-time universe: which instruments were in the index then.

        Half-open interval `[effective_from, effective_to)` (TRD §14.3b), so a
        constituent replaced on date D belongs to the old universe up to D-1 and
        the new one from D — never both, never neither.
        """
        rows = self.conn.execute(
            """SELECT DISTINCT instrument FROM index_membership
                WHERE index_name = ? AND effective_from <= ?
                  AND (effective_to IS NULL OR ? < effective_to)
                ORDER BY instrument""",
            (index_name, as_of, as_of),
        ).fetchall()
        return [row["instrument"] for row in rows]

    def ever_members(self, index_name: str) -> list[str]:
        """Every instrument that was *ever* in the index.

        Expected to be ~100-150 tickers for NIFTY-50 over 2000-2025, not 50.
        The companies that left are exactly the ones whose losses are otherwise
        invisible (TRD §14.3a).
        """
        rows = self.conn.execute(
            "SELECT DISTINCT instrument FROM index_membership WHERE index_name = ? ORDER BY instrument",
            (index_name,),
        ).fetchall()
        return [row["instrument"] for row in rows]


class ValidationFlagRepository(Repository):
    table = "data_validation_flags"

    def raise_flag(
        self,
        snapshot_id: int,
        flag_type: str,
        instrument: str | None = None,
        bar_date: str | None = None,
        observed_value: float | None = None,
        threshold: float | None = None,
        detail: str | None = None,
    ) -> int:
        return self.insert(
            snapshot_id=snapshot_id,
            flag_type=flag_type,
            instrument=instrument,
            bar_date=bar_date,
            observed_value=observed_value,
            threshold=threshold,
            detail=detail,
        )

    def pending(self, snapshot_id: int | None = None) -> list[Row]:
        sql = "SELECT * FROM data_validation_flags WHERE resolution = 'pending'"
        params: list[Any] = []
        if snapshot_id is not None:
            sql += " AND snapshot_id = ?"
            params.append(snapshot_id)
        sql += " ORDER BY snapshot_id, instrument, bar_date"
        return [self._decode(row) for row in self.conn.execute(sql, params)]  # type: ignore[misc]

    def resolve(self, flag_id: int, resolution: str, resolved_by: str) -> None:
        """Record a human's verdict on one flag.

        `pending` is not a resolution — that is the state being left, and
        accepting it here would let a snapshot be waved through by writing the
        same value back.
        """
        allowed = {"genuine_move", "missing_action_added", "data_error"}
        if resolution not in allowed:
            raise ValueError(f"resolution must be one of {sorted(allowed)}, got {resolution!r}")
        if not resolved_by:
            raise ValueError("resolved_by is required: a flag is resolved by a person, not anonymously")
        self.update(flag_id, resolution=resolution, resolved_by=resolved_by, resolved_at=utcnow_iso())

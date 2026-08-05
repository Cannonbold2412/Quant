"""A4's decision record (Backend-Schema §7, TRD §11.2/App-Flow §7), and the
five tables Stage 9's `aqrl review` gate and Stage 11's `MONITOR_DEPLOYMENT`
handler write against: `deployments`, `trades`, `health_checks`,
`lifecycle_events`, `vault_access_log` — all six tables (including
`promotions`) share one migration (`0003_promotion_lifecycle.sql`) and one
lifecycle, so one module holds every repository over them.

A4 has no execution authority — `promotions.requires_human_approval` is
always 1; nothing in A4's own handler merges a branch or moves capital. That
is exactly what `gates.approve`/`gates.reject` (Stage 9) do, the intended
first real caller of `pending_human_decision` below.
"""
from __future__ import annotations

from .base import Repository, Row

__all__ = [
    "DeploymentRepository",
    "HealthCheckRepository",
    "LifecycleEventRepository",
    "PromotionRepository",
    "TradeRepository",
    "VaultAccessRepository",
]


class PromotionRepository(Repository):
    table = "promotions"
    json_columns = frozenset({"evidence_summary"})
    updated_column = None

    def latest_for_strategy(self, strategy_id: int) -> Row | None:
        rows = self.find(strategy_id=strategy_id, order_by="id DESC", limit=1)
        return rows[0] if rows else None

    def pending_human_decision(self) -> list[Row]:
        """Every recommendation still awaiting a human — Stage 9's `aqrl
        review` is the intended first real caller of this."""
        return self.find(human_decision="pending", order_by="id")


class DeploymentRepository(Repository):
    table = "deployments"
    json_columns = frozenset({"regimes_required", "regimes_observed"})
    updated_column = "updated_at"

    def active_for_strategy(self, strategy_id: int) -> list[Row]:
        return self.find(strategy_id=strategy_id, status="active", order_by="id")

    def active(self) -> list[Row]:
        """Every deployment still being monitored, across all strategies — the
        source list for Stage 11's daily `MONITOR_DEPLOYMENT` fan-out."""
        return self.find(status="active", order_by="id")

    def record_trade_progress(self, deployment_id: int, *, new_trades: int, regimes_seen: list[str]) -> None:
        """Bump `trades_completed` and union `regimes_seen` into
        `regimes_observed` — the two PRD §9.3 gate inputs a replay produces
        incrementally, run over run, rather than recomputable from `trades`
        alone (a regime once observed stays observed even if later bars don't
        repeat it)."""
        deployment = self.get(deployment_id)
        if deployment is None:
            raise ValueError(f"no deployment {deployment_id}")
        observed = set(deployment["regimes_observed"] or [])
        observed.update(regimes_seen)
        self.update(
            deployment_id,
            trades_completed=(deployment["trades_completed"] or 0) + new_trades,
            regimes_observed=sorted(observed),
        )


class TradeRepository(Repository):
    table = "trades"

    def for_deployment(self, deployment_id: int) -> list[Row]:
        return self.find(deployment_id=deployment_id, order_by="entry_time")

    def existing_keys(self, deployment_id: int) -> set[tuple[str, str]]:
        """`(instrument, entry_time)` pairs already recorded — what makes a
        replay re-run idempotent instead of double-inserting the same trade."""
        rows = self.conn.execute(
            "SELECT instrument, entry_time FROM trades WHERE deployment_id = ?", (deployment_id,)
        ).fetchall()
        return {(row["instrument"], row["entry_time"]) for row in rows}


class HealthCheckRepository(Repository):
    table = "health_checks"

    def latest_for_deployment(self, deployment_id: int) -> Row | None:
        rows = self.find(deployment_id=deployment_id, order_by="id DESC", limit=1)
        return rows[0] if rows else None

    def for_deployment(self, deployment_id: int) -> list[Row]:
        return self.find(deployment_id=deployment_id, order_by="check_time")


class LifecycleEventRepository(Repository):
    table = "lifecycle_events"
    json_columns = frozenset({"evidence"})
    updated_column = None


class VaultAccessRepository(Repository):
    """`vault_access_log` (TRD §15.2) — "the one defence that does not
    depend on counting anything," so its own bookkeeping must be exact.

    The lifetime budget itself is not a stored counter: it is `Settings.
    vault_budget_per_family` minus `opens_for_family`, derived from this
    table's own rows rather than a second place that could drift from it.
    """

    table = "vault_access_log"
    created_column = None  # this table's timestamp column is `opened_at`, not `created_at`
    updated_column = None

    def opens_for_family(self, family: str) -> int:
        return self.count(family=family)

"""A4's decision record (Backend-Schema §7, TRD §11.2/App-Flow §7), and the
three tables Stage 9's `aqrl review` gate writes on approval (Backend-Schema
§8): `deployments`, `lifecycle_events`, `vault_access_log`.

A4 has no execution authority — `promotions.requires_human_approval` is
always 1; nothing in A4's own handler merges a branch or moves capital. That
is exactly what `gates.approve`/`gates.reject` (Stage 9) do, the intended
first real caller of `pending_human_decision` below.
"""
from __future__ import annotations

from .base import Repository, Row

__all__ = ["DeploymentRepository", "LifecycleEventRepository", "PromotionRepository", "VaultAccessRepository"]


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

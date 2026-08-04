"""A4's decision record (Backend-Schema §7, TRD §11.2/App-Flow §7).

A4 has no execution authority — this table is a recommendation, never an
action. `requires_human_approval` is always 1; nothing here merges a branch
or moves capital (that is Stage 9's `aqrl review` gate, TRD §5.3).
"""
from __future__ import annotations

from .base import Repository, Row

__all__ = ["PromotionRepository"]


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

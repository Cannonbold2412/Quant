"""`audit_log` — "why did you do this?" for every system action (PRD §11.1).

Stage 4 is the first writer: `aqrl.orchestration.states.transition` records
one row per state change, and the plan is explicit that invalid transitions
are *errors, not warnings* — this table is where the valid ones leave a trail.
"""
from __future__ import annotations

from typing import Any

from .base import Repository, Row

__all__ = ["AuditLogRepository"]


class AuditLogRepository(Repository):
    table = "audit_log"
    json_columns = frozenset({"evidence"})

    def record(
        self,
        actor: str,
        action: str,
        *,
        entity_type: str | None = None,
        entity_id: int | None = None,
        reasoning: str | None = None,
        evidence: dict[str, Any] | None = None,
        prompt_version: str | None = None,
        model_version: str | None = None,
    ) -> int:
        return self.insert(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            reasoning=reasoning,
            evidence=evidence,
            prompt_version=prompt_version,
            model_version=model_version,
        )

    def for_entity(self, entity_type: str, entity_id: int, *, limit: int | None = None) -> list[Row]:
        return self.find(entity_type=entity_type, entity_id=entity_id, order_by="id DESC", limit=limit)

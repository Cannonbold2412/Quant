"""The read side of the knowledge base A3 (Stage 6) consults.

Structured filter only — no vector search. Relevance-by-embedding is Stage
7's Research Brief requirement (Implementation_Plan §10); this exists so
`assemble_review_brief` has something to call today, and returns an empty
list until Stage 8's A5 (`Implementation_Plan.md` §11) starts writing rows.
"""
from __future__ import annotations

from .base import Repository, Row

__all__ = ["KnowledgeEntryRepository"]


class KnowledgeEntryRepository(Repository):
    table = "knowledge_entries"
    json_columns = frozenset(
        {"evidence", "applicable_markets", "applicable_timeframes", "applicable_regimes", "future_ideas"}
    )

    def relevant_to(self, strategy: Row, limit: int = 10) -> list[Row]:
        """Non-superseded entries scoped globally or to this strategy's
        family/market — structured filters only, per the module docstring.
        """
        sql = """SELECT * FROM knowledge_entries
                  WHERE superseded_by IS NULL
                    AND (scope = 'global' OR strategy_id = ?)
                  ORDER BY id DESC LIMIT ?"""
        rows = self.conn.execute(sql, (strategy.get("id"), limit)).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[misc]

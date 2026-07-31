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

    def failure_patterns(self, market: str | None, timeframe: str | None, limit: int = 20) -> list[Row]:
        """The anti-amnesia surface for Stage 7's Research Brief (App-Flow
        §3.2/§3.4) — lessons with counter-evidence, or a cross-experiment
        `pattern`/`global_rule` entry, scoped to a market/timeframe.

        SQL bounds a global candidate window, same shape as `relevant_to`;
        market/timeframe membership is checked in Python against the decoded
        `applicable_markets`/`applicable_timeframes` lists, not a `LIKE`
        against the raw JSON text — a `LIKE` match wouldn't carry over to a
        future JSONB column (TRD §20's dialect-portability rule is about the
        query's *shape*, not just its syntax).
        """
        sql = """SELECT * FROM knowledge_entries
                  WHERE superseded_by IS NULL
                    AND (counter_evidence_count > 0 OR entry_type IN ('pattern', 'global_rule'))
                  ORDER BY counter_evidence_count DESC, id DESC LIMIT ?"""
        rows = [self._decode(row) for row in self.conn.execute(sql, (limit * 5,)).fetchall()]

        def _applies(row: Row) -> bool:
            markets = row.get("applicable_markets")
            timeframes = row.get("applicable_timeframes")
            market_ok = not markets or market is None or market in markets
            timeframe_ok = not timeframes or timeframe is None or timeframe in timeframes
            return market_ok and timeframe_ok

        return [row for row in rows if row is not None and _applies(row)][:limit]

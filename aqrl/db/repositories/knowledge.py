"""The knowledge base A3/A1 consult and A5 (Stage 8) writes.

Stage 6/7 only ever needed a reader (`KnowledgeEntryRepository.relevant_to` /
`failure_patterns`), so every write path below is new. `KnowledgeEdgeRepository`
carries the one piece of genuinely non-trivial logic in this module: TRD
§12.5 says *"an edge with no experiment backing must not exist"* and that
repeated observation *"updates counts, never duplicates rows"* — both
enforced here, not left to a caller's discipline.
"""
from __future__ import annotations

from typing import Any

from .base import Repository, Row, utcnow_iso

__all__ = [
    "KnowledgeEdgeRepository",
    "KnowledgeEntryRepository",
    "LabNotebookRepository",
    "curiosity_payoff_rate",
    "repeat_failure_rate",
]


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

    def record(self, **fields: Any) -> int:
        """A5's write path (TRD §12.1, Backend-Schema §9). `future_ideas` is
        **mandatory and non-empty** — Backend-Schema §9's own comment says
        the repository enforces this, since a portable CHECK cannot see
        inside a JSON document. Raised, not coerced: an A5 output missing
        this is a bug in the brief or the model's response, not a case to
        silently paper over with `[]`.
        """
        if not fields.get("future_ideas"):
            raise ValueError("knowledge_entries.future_ideas is mandatory and must be non-empty (TRD §12.1)")
        return self.insert(**fields)

    def supersede(self, old_id: int, new_id: int) -> None:
        """Knowledge is revised, never deleted (Backend-Schema §9)."""
        self.update(old_id, superseded_by=new_id)


class KnowledgeEdgeRepository(Repository):
    """The knowledge graph (TRD §12.5). No `uid`/`created_at` — the table
    carries `first_observed_at`/`last_updated_at` instead, same no-timestamp-
    convention switch-off `EvaluationTestRepository` already uses."""

    table = "knowledge_edges"
    json_columns = frozenset({"supporting_experiments"})
    has_uid = False
    created_column = None

    def _existing(self, subject: str, predicate: str, object_: str) -> Row | None:
        rows = self.find(subject=subject, predicate=predicate, object=object_)
        return rows[0] if rows else None

    def observe(
        self,
        subject: str,
        predicate: str,
        object_: str,
        *,
        experiment_ids: list[int],
        supports: bool = True,
        confidence: float | None = None,
    ) -> int:
        """Record one observation. First sight inserts; repeat sight updates
        counts and merges `supporting_experiments` — never a duplicate row,
        matching the table's own `UNIQUE (subject, predicate, object)`.

        `experiment_ids` must be non-empty: **"an edge with no experiment
        backing must not exist"** (TRD §12.5) is enforced here, not left to
        a caller's discipline — no agent may assert a relationship from pure
        reasoning alone.
        """
        if not experiment_ids:
            raise ValueError(
                f"refusing to record edge ({subject!r}, {predicate!r}, {object_!r}) with no "
                "backing experiments (TRD §12.5: an edge with no experiment backing must not exist)"
            )
        now = utcnow_iso()
        existing = self._existing(subject, predicate, object_)
        if existing is None:
            return self.insert(
                subject=subject,
                predicate=predicate,
                object=object_,
                confidence=confidence,
                evidence_count=1 if supports else 0,
                counter_evidence_count=0 if supports else 1,
                supporting_experiments=list(experiment_ids),
                first_observed_at=now,
                last_updated_at=now,
            )

        merged = sorted(set(existing.get("supporting_experiments") or []) | set(experiment_ids))
        self.update(
            existing["id"],
            evidence_count=existing["evidence_count"] + (1 if supports else 0),
            counter_evidence_count=existing["counter_evidence_count"] + (0 if supports else 1),
            supporting_experiments=merged,
            confidence=confidence if confidence is not None else existing.get("confidence"),
            last_updated_at=now,
        )
        return int(existing["id"])


class LabNotebookRepository(Repository):
    """The human-readable per-strategy record (TRD §16, Backend-Schema §9)."""

    table = "lab_notebooks"
    json_columns = frozenset({"next_questions"})

    def insert(self, **fields: Any) -> int:  # type: ignore[override]
        """`next_questions` is **mandatory, non-empty** — enforced here for
        the same reason `KnowledgeEntryRepository.record` enforces
        `future_ideas`: a portable CHECK cannot see inside JSON."""
        if not fields.get("next_questions"):
            raise ValueError("lab_notebooks.next_questions is mandatory and must be non-empty (TRD §16)")
        return super().insert(**fields)

    def for_strategy(self, strategy_id: int) -> list[Row]:
        return self.find(strategy_id=strategy_id, order_by="id")


def repeat_failure_rate(conn, *, since: str | None = None) -> dict[str, Any]:
    """Implementation_Plan §11's done-when metric: *"rejected experiments
    demonstrably prevent similar future proposals ... measured by the
    repeat-failure rate trending toward zero."*

    An experiment counts as a **repeat** when its `failure_reason` was
    already recorded — before its own spec was proposed — by a
    non-superseded `knowledge_entries` row applicable to its market/
    timeframe. "Already recorded" is read from `evidence.failure_reasons`,
    a small structured list A5's `ARCHIVE`/`MINE_PATTERNS` handlers write
    into every entry's free-form `evidence` JSON (`handlers/archive.py`) —
    `statement` is deliberately prose ("ATR multipliers above 3.0
    consistently overfit"), not an enum value, so matching against it would
    be a fragile substring guess. This scans only entries this same
    Stage 8 code populated that field for.

    Scoped to market/timeframe only, matching the precedent
    `KnowledgeEntryRepository.failure_patterns` already set for A1's brief —
    `knowledge_entries` carries no dedicated family column to scope on
    beyond it (only `strategy_id`, which names one strategy, not a family).

    Only experiments with a genuine research `failure_reason` are counted —
    the three bug categories (`code_error`, `look_ahead_detected`,
    `data_leakage_detected`) route back to A2 and are never research
    conclusions (Backend-Schema §14.5), so they are excluded here too.
    """
    sql = """
        SELECT e.id, e.failure_reason, sp.created_at AS spec_created_at,
               s.market, s.timeframe
          FROM experiments e
          JOIN strategies s ON s.id = e.strategy_id
          LEFT JOIN strategy_specs sp ON sp.id = e.spec_id
         WHERE e.failure_reason IS NOT NULL
           AND e.failure_reason NOT IN ('code_error', 'look_ahead_detected', 'data_leakage_detected')
    """
    params: list[Any] = []
    if since is not None:
        sql += " AND e.created_at >= ?"
        params.append(since)
    experiments = conn.execute(sql, params).fetchall()

    entries = conn.execute(
        """SELECT evidence, applicable_markets, applicable_timeframes, created_at
             FROM knowledge_entries
            WHERE superseded_by IS NULL"""
    ).fetchall()
    entry_repo = KnowledgeEntryRepository(conn)
    decoded_entries = [entry_repo._decode(row) for row in entries]

    total = 0
    repeats = 0
    for row in experiments:
        total += 1
        reason = row["failure_reason"]
        market, timeframe = row["market"], row["timeframe"]
        # No spec on record (e.g. a hand-seeded fixture) — nothing to compare
        # "already recorded before" against, so it cannot be a repeat.
        cutoff = row["spec_created_at"]
        if cutoff is None:
            continue
        for entry in decoded_entries:
            if entry is None or entry["created_at"] >= cutoff:
                continue
            evidence = entry.get("evidence") or {}
            if not isinstance(evidence, dict) or reason not in (evidence.get("failure_reasons") or []):
                continue
            markets = entry.get("applicable_markets")
            timeframes = entry.get("applicable_timeframes")
            if markets and market not in markets:
                continue
            if timeframes and timeframe not in timeframes:
                continue
            repeats += 1
            break

    rate = repeats / total if total else 0.0
    return {"repeats": repeats, "total": total, "rate": rate}


def curiosity_payoff_rate(conn, *, since: str | None = None) -> dict[str, Any]:
    """Stage 10's done-when metric, the counterpart to `repeat_failure_rate`
    above: *"did asking this question ever pay off?"* (TRD §12.4).

    A `research_questions` row counts as answered once a collector's find
    reaches `EXTRACT_KNOWLEDGE` (`ResearchQuestionRepository.record_answer`)
    and as *paid off* once one of its answers is cited in a spec A1
    proposes (`record_produced_spec`, `handlers/generate.py`). Scoped to
    every row ever pushed, not just `open` ones, since a question's
    lifecycle (`open` -> `searching` -> `answered`) has already moved past
    `open` by the time it could possibly have paid off.
    """
    sql = "SELECT status, produced_spec_ids FROM research_questions"
    params: list[Any] = []
    if since is not None:
        sql += " WHERE created_at >= ?"
        params.append(since)
    rows = conn.execute(sql, params).fetchall()

    total = len(rows)
    answered = 0
    paid_off = 0
    for row in rows:
        if row["status"] in ("answered",):
            answered += 1
        raw = row["produced_spec_ids"]
        if raw and raw not in ("[]", "null"):
            paid_off += 1

    rate = paid_off / total if total else 0.0
    return {"total": total, "answered": answered, "paid_off": paid_off, "rate": rate}

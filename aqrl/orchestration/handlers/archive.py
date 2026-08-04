"""The `ARCHIVE` and `MINE_PATTERNS` handlers — Stage 8's A5
(Implementation_Plan §11).

**Two jobs, one output shape.** `ARCHIVE` runs once per strategy, over its
complete story (TRD §12.1); `MINE_PATTERNS` runs weekly, cross-experiment,
with no single strategy to narrate. Both call `KnowledgeSession.archive` and
both write through the same repositories — the only difference is what the
brief contains and whether a `lab_notebooks` row is required. Sharing one
module mirrors how `implement.py` serves both `IMPLEMENT` and `FIX_CODE`.

**`ARCHIVE` is reached from two routes** — `review.py`'s
`REVIEW_PLATEAU_OR_REJECT` (never cleared the bar) and `promote.py`'s
`PROMOTION_DECIDED` (cleared it, whatever A4 then decided). Either way this
strategy's story has concluded, and the idempotency guard in `run()` (a
`lab_notebooks` row already exists) makes a second `ARCHIVE` for the same
strategy — which cannot happen on the current wiring, but would if a future
route ever emitted a second one — a no-op rather than a duplicate record.

**Repeat-failure rate depends on what gets written here.**
`db.repositories.knowledge.repeat_failure_rate` reads
`knowledge_entries.evidence.failure_reasons`, populated only by
`_write_knowledge` below — the metric is only as good as A5 actually naming
the structured reason a lesson documents.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ...agents.context import (
    archive_prompt_version,
    assemble_archive_brief,
    assemble_mine_brief,
    mine_prompt_version,
)
from ...agents.session import KnowledgeSession, ProposedKnowledge
from ...db.repositories import (
    EvaluationRepository,
    ExperimentRepository,
    KnowledgeEdgeRepository,
    KnowledgeEntryRepository,
    LabNotebookRepository,
    PromotionRepository,
    ResearchPlanRepository,
    ResearchQuestionRepository,
    SpecRepository,
    StrategyRepository,
)
from ...db.repositories.base import Row
from ..budgets import consume as budget_consume
from ..events import Event, emit
from ..states import transition
from .base import HandlerResult

__all__ = [
    "ArchiveOutcome",
    "MineOutcome",
    "persist_archive",
    "persist_mine",
    "run_archive",
    "run_mine",
    "set_session",
]

_session_override: KnowledgeSession | None = None


def set_session(session: KnowledgeSession | None) -> None:
    """Override the session `archive()` calls. Tests only.

    `None` restores the default — a lazily-constructed `AnthropicSession`, so
    importing or calling this handler never requires `ANTHROPIC_API_KEY`
    unless a real (non-stale) call actually happens.
    """
    global _session_override
    _session_override = session


def _get_session() -> KnowledgeSession:
    if _session_override is not None:
        return _session_override
    from ...agents.session import AnthropicSession

    return AnthropicSession()


# -- shared write path ---------------------------------------------------------


def _write_knowledge(
    conn: sqlite3.Connection, knowledge: ProposedKnowledge, *, strategy_id: int | None, experiment_id: int | None
) -> tuple[list[int], list[int], list[int]]:
    """Insert `entries`/`edges`/`questions`. Shared by `persist_archive` and
    `persist_mine` — the only difference is `strategy_id`/`experiment_id`:
    set for a per-strategy entry (`ARCHIVE`), `None` for a cross-experiment
    one (`MINE_PATTERNS`, Backend-Schema §9: "cross-experiment lessons have
    neither")."""
    entries = KnowledgeEntryRepository(conn)
    edges = KnowledgeEdgeRepository(conn)
    questions = ResearchQuestionRepository(conn)

    entry_ids: list[int] = []
    for draft in knowledge.entries:
        entry_id = entries.record(
            experiment_id=experiment_id,
            strategy_id=strategy_id,
            entry_type=draft.entry_type,
            scope=draft.scope,
            title=draft.title,
            statement=draft.statement,
            # `failure_reasons` is what `repeat_failure_rate` reads back —
            # the structured half of this otherwise free-form JSON field.
            evidence={
                "experiment_ids": draft.evidence_experiment_ids,
                "failure_reasons": draft.documents_failure_reasons,
            },
            evidence_count=len(draft.evidence_experiment_ids),
            confidence=draft.confidence,
            applicable_markets=draft.applicable_markets,
            applicable_timeframes=draft.applicable_timeframes,
            applicable_regimes=draft.applicable_regimes,
            future_ideas=draft.future_ideas,
        )
        entry_ids.append(entry_id)
        if draft.supersedes_existing_id is not None:
            entries.supersede(draft.supersedes_existing_id, entry_id)

    edge_ids = [
        edges.observe(
            draft.subject,
            draft.predicate,
            draft.object,
            experiment_ids=draft.evidence_experiment_ids,
            supports=draft.supports,
            confidence=draft.confidence,
        )
        for draft in knowledge.edges
    ]

    question_ids = [
        questions.push(
            draft.question,
            motivation=draft.motivation,
            origin_type="pattern_detection" if strategy_id is None else "experiment_failure",
            origin_experiment_id=experiment_id,
            priority=draft.priority,
        )
        for draft in knowledge.questions
    ]

    return entry_ids, edge_ids, question_ids


# -- ARCHIVE (per-strategy, TRD §12.1) -----------------------------------------


@dataclass(frozen=True)
class ArchiveOutcome:
    stale: bool
    strategy_id: int
    final_experiment_id: int
    knowledge: ProposedKnowledge | None = None
    prompt_version: str | None = None
    tokens_spent: int = 0


def run_archive(conn: sqlite3.Connection, job: Row) -> ArchiveOutcome:
    """The heavy, side-effect-free-on-the-database half."""
    strategy_id = job["strategy_id"]
    final_experiment_id = job["experiment_id"]
    if strategy_id is None or final_experiment_id is None:
        raise ValueError("ARCHIVE job requires both strategy_id and experiment_id")

    # A5 runs once per strategy (TRD §12.1) — a second ARCHIVE for the same
    # strategy (both routes can technically fire one) is a no-op.
    if LabNotebookRepository(conn).for_strategy(strategy_id):
        return ArchiveOutcome(stale=True, strategy_id=strategy_id, final_experiment_id=final_experiment_id)

    strategies = StrategyRepository(conn)
    strategy = strategies.get(strategy_id)
    if strategy is None:
        raise ValueError(f"no strategy {strategy_id}")

    experiments = ExperimentRepository(conn)
    final_experiment = experiments.get(final_experiment_id)
    if final_experiment is None:
        raise ValueError(f"no experiment {final_experiment_id}")

    spec = SpecRepository(conn).load_spec(final_experiment["spec_id"])
    experiment_history = experiments.find(strategy_id=strategy_id, order_by="iteration")

    evaluations_repo = EvaluationRepository(conn)
    all_evaluations = [
        row for experiment in experiment_history for row in evaluations_repo.for_experiment(experiment["id"])
    ]
    plans = ResearchPlanRepository(conn).find(strategy_id=strategy_id, order_by="id")
    promotion = PromotionRepository(conn).latest_for_strategy(strategy_id)

    brief = assemble_archive_brief(
        strategy=strategy,
        spec=spec,
        experiment_history=experiment_history,
        evaluations=all_evaluations,
        plans=plans,
        promotion=promotion,
    )
    prompt_version = archive_prompt_version()
    response = _get_session().archive(brief, prompt_version=prompt_version)
    knowledge = response.knowledge
    if knowledge.notebook is None:
        raise ValueError("ARCHIVE requires a lab_notebooks draft; KnowledgeSession returned none")

    return ArchiveOutcome(
        stale=False,
        strategy_id=strategy_id,
        final_experiment_id=final_experiment_id,
        knowledge=knowledge,
        prompt_version=prompt_version,
        tokens_spent=response.tokens_spent,
    )


def persist_archive(conn: sqlite3.Connection, job: Row, outcome: ArchiveOutcome) -> HandlerResult:
    """The short, atomic half: every database write for `ARCHIVE`."""
    if outcome.stale:
        return HandlerResult(tokens_spent=0)

    assert outcome.knowledge is not None and outcome.knowledge.notebook is not None  # narrows for the type checker
    notebook = outcome.knowledge.notebook

    rendered = (
        f"# {outcome.strategy_id}\n\n"
        f"**Hypothesis:** {notebook.hypothesis}\n\n"
        f"**Result:** {notebook.result}\n\n"
        f"**Reason:** {notebook.reason}\n\n"
        f"**Evidence:** {notebook.evidence}\n\n"
        f"**Confidence:** {notebook.confidence}\n\n"
        "**Next questions:**\n" + "\n".join(f"- {q}" for q in notebook.next_questions)
    )
    LabNotebookRepository(conn).insert(
        experiment_id=outcome.final_experiment_id,
        strategy_id=outcome.strategy_id,
        hypothesis=notebook.hypothesis,
        result=notebook.result,
        reason=notebook.reason,
        evidence=notebook.evidence,
        confidence=notebook.confidence,
        next_questions=notebook.next_questions,
        rendered_markdown=rendered,
    )

    _write_knowledge(
        conn, outcome.knowledge, strategy_id=outcome.strategy_id, experiment_id=outcome.final_experiment_id
    )

    # Every experiment this strategy ever ran reaches its terminal state —
    # the `archived` state `EXPERIMENT_TRANSITIONS` already defines and
    # nothing before this stage ever reached (Backend-Schema §14.2).
    experiments = ExperimentRepository(conn)
    for experiment in experiments.find(strategy_id=outcome.strategy_id, order_by="iteration"):
        if experiment["status"] != "archived":
            transition(conn, "experiments", experiment["id"], "archived", actor="agent:A5")

    budget_consume(conn, "strategy", "tokens", "day", outcome.tokens_spent, scope_id=outcome.strategy_id)
    return HandlerResult(tokens_spent=outcome.tokens_spent)


# -- MINE_PATTERNS (cross-experiment, weekly, TRD §12.1) -----------------------


@dataclass(frozen=True)
class MineOutcome:
    knowledge: ProposedKnowledge
    prompt_version: str
    tokens_spent: int
    family_groups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


#: Backend-Schema §14.5's research-finding half of `failure_reason` — the
#: three bug categories never pollute the knowledge base (TRD §10.1), same
#: exclusion `db.repositories.knowledge.repeat_failure_rate` applies.
_BUG_FAILURE_REASONS = frozenset({"code_error", "look_ahead_detected", "data_leakage_detected"})


def _recent_family_failure_groups(conn: sqlite3.Connection, *, limit_per_family: int = 50) -> dict[str, list[dict]]:
    """Recently-closed experiments' `failure_reason`s, grouped by family —
    the candidate pool A5 looks for a repeated pattern across (App-Flow
    §8.2)."""
    rows = conn.execute(
        """SELECT s.family, e.id AS experiment_id, e.failure_reason, e.outcome, e.completed_at
             FROM experiments e
             JOIN strategies s ON s.id = e.strategy_id
            WHERE e.failure_reason IS NOT NULL
            ORDER BY e.completed_at DESC"""
    ).fetchall()
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if row["failure_reason"] in _BUG_FAILURE_REASONS:
            continue
        bucket = groups.setdefault(row["family"], [])
        if len(bucket) < limit_per_family:
            bucket.append(
                {"experiment_id": row["experiment_id"], "failure_reason": row["failure_reason"], "outcome": row["outcome"]}
            )
    return groups


def run_mine(conn: sqlite3.Connection, job: Row) -> MineOutcome:
    """The heavy, side-effect-free-on-the-database half. Ignores the job's
    own payload — `MINE_PATTERNS` is cross-experiment, not scoped to one
    strategy or experiment (`scheduler.TIME_DRIVEN_SCHEDULE`'s weekly entry
    fires it with an empty payload)."""
    groups = _recent_family_failure_groups(conn)
    existing_entries = KnowledgeEntryRepository(conn).find(
        superseded_by=None, order_by="id DESC", limit=100
    )
    # Only family/market/global entries belong in this job's context — an
    # `'experiment'`-scoped one is per-strategy archival's own output.
    existing_entries = [row for row in existing_entries if row.get("scope") != "experiment"]

    brief = assemble_mine_brief(family_failure_groups=groups, existing_entries=existing_entries)
    prompt_version = mine_prompt_version()
    response = _get_session().archive(brief, prompt_version=prompt_version)
    return MineOutcome(
        knowledge=response.knowledge,
        prompt_version=prompt_version,
        tokens_spent=response.tokens_spent,
        family_groups=groups,
    )


def persist_mine(conn: sqlite3.Connection, job: Row, outcome: MineOutcome) -> HandlerResult:
    """The short, atomic half: every database write for `MINE_PATTERNS`."""
    entry_ids, _edge_ids, _question_ids = _write_knowledge(
        conn, outcome.knowledge, strategy_id=None, experiment_id=None
    )

    # A found pattern is a real event (TRD §4.1's FAILURE_PATTERN_DETECTED,
    # whose job_type is already None — a direct audit record, not a queued
    # job, per `events.py`'s own table) — fired once per entry mined, not
    # once for the whole (possibly empty) batch.
    for entry_id, draft in zip(entry_ids, outcome.knowledge.entries, strict=True):
        emit(
            conn,
            Event.FAILURE_PATTERN_DETECTED,
            payload={"knowledge_entry_id": entry_id, "title": draft.title},
            dedupe_key=f"failure_pattern_detected:{entry_id}",
            reasoning=draft.statement,
        )

    budget_consume(conn, "global", "tokens", "day", outcome.tokens_spent)
    return HandlerResult(tokens_spent=outcome.tokens_spent)

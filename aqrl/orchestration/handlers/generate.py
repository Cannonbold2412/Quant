"""The `GENERATE_SPEC` handler — Stage 7's A1 (Implementation_Plan §10).

**A1 proposes identity, not just a spec.** Unlike `IMPLEMENT`/`REVIEW`,
which act on a strategy that already exists, `GENERATE_SPEC` has no strategy
yet — `run()` reads a `research_goals` row, not a `strategy_id`, and A1's
output (`ProposedHypothesis`) names the strategy's `name`/`family`/`market`/
`timeframe` as part of proposing it. `persist()` is the exact point where
Stage 7's output becomes Stage 5's input: it emits `Event.SPEC_SAVED` with
the same payload shape `aqrl strategy new` / `implement.py` already expect,
so `IMPLEMENT` runs unmodified from a hypothesis A1 wrote.

**Duplicate rejection is checked in Python, before persisting anything**
(App-Flow §3.4) — `SpecRepository.by_hash` for an exact match (reject, spend
no further compute), `SpecRepository.find_near_duplicates` for a structural
near-match (proceed — it *is* different — but record it on the audit log
rather than silently losing the connection).

**Same `run`/`persist` split as `implement.py`/`review.py`, for the same
reason**: a multi-minute A1 call (and the embedding calls
`research_brief.top_k_relevant` makes on its behalf) must never hold
SQLite's write lock.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ...agents.context import assemble_generate_brief, generate_prompt_version
from ...agents.embeddings import Embedder
from ...agents.research_brief import top_k_relevant
from ...agents.session import HypothesisSession, ProposedHypothesis
from ...db.repositories import (
    AuditLogRepository,
    KnowledgeEntryRepository,
    ResearchGoalRepository,
    ResearchQuestionRepository,
    SpecRepository,
    StrategyRepository,
)
from ...db.repositories.base import Row
from ...operators.spec import StrategySpec
from ...profiles import ProfileLoader
from ..events import Event, emit
from ..states import transition
from .base import HandlerResult

__all__ = ["GenerateOutcome", "persist", "run", "set_embedder", "set_session"]

_session_override: HypothesisSession | None = None
_embedder_override: Embedder | None = None


def set_session(session: HypothesisSession | None) -> None:
    """Override the session `generate()` calls. Tests only.

    `None` restores the default — a lazily-constructed `AnthropicSession`,
    so importing or calling this handler never requires `ANTHROPIC_API_KEY`
    unless a real (non-forced-stop) generation actually happens.
    """
    global _session_override
    _session_override = session


def _get_session() -> HypothesisSession:
    if _session_override is not None:
        return _session_override
    from ...agents.session import AnthropicSession

    return AnthropicSession()


def set_embedder(embedder: Embedder | None) -> None:
    """Override the embedder `top_k_relevant` calls. Tests only. `None`
    restores the default — a lazily-constructed `VoyageEmbedder`, so nothing
    requires `VOYAGE_API_KEY` unless a real relevance search actually runs.
    """
    global _embedder_override
    _embedder_override = embedder


def _get_embedder() -> Embedder:
    if _embedder_override is not None:
        return _embedder_override
    from ...agents.embeddings import VoyageEmbedder

    return VoyageEmbedder()


@dataclass(frozen=True)
class GenerateOutcome:
    forced_stop: bool
    goal_id: int
    reason: str | None = None  # forced-stop reason; None otherwise
    hypothesis: ProposedHypothesis | None = None
    spec: StrategySpec | None = None
    spec_hash: str | None = None
    exact_duplicate: bool = False
    duplicate_of: Row | None = None  # the existing strategy_specs row, if exact_duplicate
    near_duplicates: list[dict[str, Any]] = field(default_factory=list)
    prompt_version: str | None = None
    tokens_spent: int = 0
    eval_payload: dict[str, Any] = field(default_factory=dict)


def run(conn: sqlite3.Connection, job: Row) -> GenerateOutcome:
    """The heavy, side-effect-free-on-the-database half.

    Reads the database (and may call Claude, plus the embedder for relevance
    search) but writes nothing to the job's own business state — every
    insert and state transition happens in `persist()`. `research_brief.
    top_k_relevant` does cache embeddings as it goes, the same kind of
    real-but-idempotent side effect `implement.py`'s git commit is: safe to
    repeat, never gated behind the job's write lock.
    """
    payload = job["payload"] or {}
    goal_id = payload.get("goal_id")
    if goal_id is None:
        raise ValueError("GENERATE_SPEC job has no 'goal_id' in its payload")

    goals = ResearchGoalRepository(conn)
    goal = goals.get(goal_id)
    if goal is None:
        raise ValueError(f"no research_goals row {goal_id}")

    forced_reason: str | None = None
    if goal["status"] != "active":
        forced_reason = f"research_goals {goal_id} is {goal['status']!r}, not 'active'"
    elif goal["hypothesis_budget"] is not None and goal["hypotheses_used"] >= goal["hypothesis_budget"]:
        forced_reason = (
            f"hypotheses_used {goal['hypotheses_used']} reached hypothesis_budget "
            f"({goal['hypothesis_budget']})"
        )

    if forced_reason is not None:
        return GenerateOutcome(forced_stop=True, goal_id=goal_id, reason=forced_reason)

    embedder = _get_embedder()
    query_text = f"{goal.get('title') or ''}\n{goal.get('description') or ''}"
    relevant_external = top_k_relevant(conn, embedder, table="external_knowledge", query_text=query_text)
    relevant_internal = top_k_relevant(conn, embedder, table="knowledge_entries", query_text=query_text)
    open_questions = ResearchQuestionRepository(conn).open_questions()
    market, timeframe = goal.get("market"), goal.get("timeframe")
    failure_patterns = KnowledgeEntryRepository(conn).failure_patterns(market, timeframe)
    family_trial_counts = (
        StrategyRepository(conn).family_trial_counts_for(market, timeframe) if market and timeframe else {}
    )

    brief = assemble_generate_brief(
        goal=goal,
        relevant_external_knowledge=relevant_external,
        relevant_internal_knowledge=relevant_internal,
        open_questions=open_questions,
        failure_patterns=failure_patterns,
        family_trial_counts=family_trial_counts,
    )
    prompt_version = generate_prompt_version()
    response = _get_session().generate(brief, prompt_version=prompt_version)
    hypothesis = response.hypothesis
    spec = hypothesis.to_spec()
    spec.validate_spec()
    digest = spec.spec_hash()

    existing = SpecRepository(conn).by_hash(digest)
    if existing is not None:
        # Exact match: App-Flow §3.4's "REJECT, log duplicate, spend no
        # further compute" — no strategy row, no near-duplicate lookup
        # (which needs a family to scope to, and there's nothing more to
        # learn from it once an exact match already exists).
        return GenerateOutcome(
            forced_stop=False,
            goal_id=goal_id,
            hypothesis=hypothesis,
            spec=spec,
            spec_hash=digest,
            exact_duplicate=True,
            duplicate_of=existing,
            prompt_version=prompt_version,
            tokens_spent=response.tokens_spent,
        )

    near_duplicates = SpecRepository(conn).find_near_duplicates(spec, hypothesis.family)

    asset_class = payload.get("asset_class")
    if not asset_class:
        # No column on `research_goals` carries this (a market can declare
        # several), and the enqueuer (nightly batch, novelty push) may not
        # have supplied one — the closest-available-bucket fallback
        # `implement.py`'s own provenance handling already established for
        # ambiguous mappings: the market's first declared asset class.
        asset_class = ProfileLoader().load_market(hypothesis.market).asset_classes[0]

    eval_payload = {k: v for k, v in payload.items() if k not in {"goal_id", "asset_class"}}
    eval_payload["asset_class"] = asset_class

    return GenerateOutcome(
        forced_stop=False,
        goal_id=goal_id,
        hypothesis=hypothesis,
        spec=spec,
        spec_hash=digest,
        exact_duplicate=False,
        near_duplicates=near_duplicates,
        prompt_version=prompt_version,
        tokens_spent=response.tokens_spent,
        eval_payload=eval_payload,
    )


def persist(conn: sqlite3.Connection, job: Row, outcome: GenerateOutcome) -> HandlerResult:
    """The short, atomic half: every database write, in one transaction."""
    if outcome.forced_stop:
        AuditLogRepository(conn).record(
            actor="agent:A1",
            action="generate_spec.forced_stop",
            entity_type="research_goals",
            entity_id=outcome.goal_id,
            reasoning=outcome.reason,
        )
        return HandlerResult(tokens_spent=0)

    # The A1 call happened either way — App-Flow §3.4's "spend no further
    # compute" is about the downstream IMPLEMENT/EVALUATE compute an exact
    # duplicate avoids, not the call that already ran and already spent
    # `outcome.tokens_spent`. The budget counts attempts, not novel ideas.
    ResearchGoalRepository(conn).increment_hypotheses_used(outcome.goal_id)

    if outcome.exact_duplicate:
        AuditLogRepository(conn).record(
            actor="agent:A1",
            action="generate_spec.exact_duplicate",
            entity_type="strategy_specs",
            entity_id=outcome.duplicate_of["id"] if outcome.duplicate_of else None,
            reasoning=f"spec_hash {outcome.spec_hash} already exists; rejected before further compute",
            evidence={"spec_hash": outcome.spec_hash},
        )
        return HandlerResult(tokens_spent=outcome.tokens_spent)

    assert outcome.hypothesis is not None and outcome.spec is not None  # narrows for the type checker
    hypothesis = outcome.hypothesis

    strategies = StrategyRepository(conn)
    specs = SpecRepository(conn)

    strategy_id = strategies.get_or_create(
        hypothesis.name, hypothesis.family, hypothesis.market, hypothesis.timeframe
    )
    spec_id = specs.insert_spec(
        outcome.spec,
        strategy_id,
        prompt_version=outcome.prompt_version,
        source_external_knowledge_ids=hypothesis.source_external_knowledge_ids,
        source_internal_knowledge_ids=hypothesis.source_internal_knowledge_ids,
    )

    strategy = strategies.get(strategy_id)
    if strategy["status"] == "draft":
        transition(conn, "strategies", strategy_id, "spec_ready", actor="agent:A1")

    if outcome.near_duplicates:
        # Structural near-duplicate: proceeds as novel (it *is* structurally
        # different), but the connection is recorded on this strategy's
        # audit trail rather than wired into a future A3's brief — A3 reads
        # its own strategy's history, not sibling strategies' (Stage 7's
        # own scope note; see the plan for why this stops at the audit log).
        AuditLogRepository(conn).record(
            actor="agent:A1",
            action="generate_spec.near_duplicate",
            entity_type="strategies",
            entity_id=strategy_id,
            reasoning=(
                f"structurally close to {len(outcome.near_duplicates)} existing spec(s) "
                f"in family {hypothesis.family!r}"
            ),
            evidence={"near_duplicates": outcome.near_duplicates},
        )

    emit(
        conn,
        Event.SPEC_SAVED,
        strategy_id=strategy_id,
        payload={**outcome.eval_payload, "spec_id": spec_id},
    )

    return HandlerResult(tokens_spent=outcome.tokens_spent)

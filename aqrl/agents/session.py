"""The Claude session wrapper — stateless, schema-validated (TRD §16).

**Stateless and disposable.** Every call is a fresh `AgentSession.propose_spec`
or `ReviewSession.review` with a complete brief; nothing here remembers a
previous call, matching TRD §16's *"Model: Claude, via Claude Code sessions.
Stateless and disposable."* Context assembly is `context.py`'s job, never
this module's — a session wrapper that goes looking for its own context is
exactly what TRD §16 rules out (*"Claude does not go hunting for context"*).

**Structured outputs, not prose parsing.** `ProposedSpec` (A2) and
`ProposedPlan` (A3) are the only two shapes Claude may return here: an
operator DAG plus a plain-language `change_summary` for A2 — never free code
(App-Flow §4.2: *"translation, not invention"*) — and a verdict plus
plain-language `proposed_changes` for A3 — never code either (App-Flow
§6.4: *"the research plan is not code"*). The real implementation
(`AnthropicSession`) uses `client.messages.parse(..., output_format=...)`,
so the SDK — not a hand-rolled JSON parser — is what guarantees a malformed
response never reaches the render/check pipeline or the `research_plans`
table.

**Three implementations, two protocols.** `AnthropicSession` implements both
`AgentSession` (A2) and `ReviewSession` (A3) and is the only one that touches
the network; `StubSession`/`ReplaySession` and their A3 counterparts
`StubReviewSession`/`ReplayReviewSession` return pre-supplied responses so
every test in this codebase — including the full offline loop test — never
needs `ANTHROPIC_API_KEY` or a live connection. Retries are the SDK's own
(`max_retries`, default 2, on 429/5xx/connection errors) plus
`aqrl.orchestration.failures.classify`, which already lists
`RateLimitError`/`APITimeoutError`/`APIConnectionError` as transient — this
module does not duplicate that logic, it just lets those exceptions propagate
to the worker, exactly like any other handler failure.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..operators.spec import Node, StrategySpec

__all__ = [
    "AgentResponse",
    "AgentSession",
    "AnthropicSession",
    "HypothesisResponse",
    "HypothesisSession",
    "KnowledgeEdgeDraft",
    "KnowledgeEntryDraft",
    "KnowledgeResponse",
    "KnowledgeSession",
    "LabNotebookDraft",
    "PromotionResponse",
    "PromotionSession",
    "ProposedHypothesis",
    "ProposedKnowledge",
    "ProposedPlan",
    "ProposedPromotion",
    "ProposedSpec",
    "ReplayHypothesisSession",
    "ReplayKnowledgeSession",
    "ReplayPromotionSession",
    "ReplayReviewSession",
    "ReplaySession",
    "ResearchQuestionDraft",
    "ReviewResponse",
    "ReviewSession",
    "StubHypothesisSession",
    "StubKnowledgeSession",
    "StubPromotionSession",
    "StubReviewSession",
    "StubSession",
]

#: Backend-Schema §14.5's *research-finding* half of `experiments.failure_reason`
#: — the three bug categories (`code_error`, `look_ahead_detected`,
#: `data_leakage_detected`) are deliberately excluded: they route back to A2
#: and must never be recorded as a research conclusion, so A5 can never cite
#: one as what a knowledge entry documents.
ResearchFailureReason = Literal[
    "no_signal",
    "negative_expectancy",
    "costs_exceed_edge",
    "overfit_in_sample",
    "walk_forward_unstable",
    "regime_dependent",
    "pbo_too_high",
    "deflated_sharpe_insufficient",
    "insufficient_trades",
    "monte_carlo_ruin_risk",
    "parameter_sensitive",
    "capacity_constrained",
    "plateaued_below_bar",
]


class ProposedSpec(BaseModel):
    """A2's one output shape — an operator DAG plus its plain-language why.

    Mirrors `StrategySpec` field-for-field (`to_spec()` is the conversion),
    with one addition: `change_summary`, A2's plain-language description of
    what changed and why — including any objection to the plan it implemented
    anyway (App-Flow §4.3: *"the objection surfaces to A3, never acted on
    unilaterally"*). `extra="forbid"` matters here specifically: a field the
    model invents that this schema does not expect must fail loudly, not be
    silently dropped and quietly change what got implemented.
    """

    model_config = ConfigDict(extra="forbid")

    entry_logic: list[Node] = Field(default_factory=list)
    exit_logic: list[Node] = Field(default_factory=list)
    filter_logic: list[Node] = Field(default_factory=list)
    risk_logic: list[Node] = Field(default_factory=list)
    universe: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    hypothesis: str
    rationale: str | None = None
    expected_behavior: str | None = None
    change_summary: str

    def to_spec(self) -> StrategySpec:
        return StrategySpec(
            entry_logic=self.entry_logic,
            exit_logic=self.exit_logic,
            filter_logic=self.filter_logic,
            risk_logic=self.risk_logic,
            universe=self.universe,
            parameters=self.parameters,
            hypothesis=self.hypothesis,
            rationale=self.rationale,
            expected_behavior=self.expected_behavior,
        )


class ProposedPlan(BaseModel):
    """A3's one output shape — a verdict plus its plain-language reasoning.

    Mirrors `research_plans` field-for-field. **Never contains code** — App-Flow
    §6.4: A3 says *"replace the fixed stop with an ATR trailing stop"*, it
    does not write the function. `verdict` is closed to the three values
    App-Flow §6.2 allows below the bar; there is deliberately no `promote`
    option here — clearing the bar is a Python short-circuit A3 is never
    consulted on (App-Flow §6.1). `extra="forbid"` for the same reason as
    `ProposedSpec`: an invented field must fail loudly, not be silently
    dropped.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["iterate", "plateau", "reject"]
    diagnosis: str
    evidence_cited: list[dict[str, Any]] = Field(default_factory=list)
    proposed_changes: list[dict[str, Any]] = Field(default_factory=list)
    expected_effect: str | None = None
    confidence: float | None = None


class ProposedHypothesis(BaseModel):
    """A1's one output shape — a brand-new strategy, identity and all
    (Implementation_Plan §10, App-Flow §3.4).

    Unlike `ProposedSpec`/`ProposedPlan`, which revise or review an
    *existing* strategy, A1 has none to revise — it proposes the identity
    (`name`/`family`/`market`/`timeframe`) alongside the operator DAG in the
    same call. Every `ProposedSpec` field carries over unchanged (mirrors
    `StrategySpec` field-for-field, `to_spec()` is the same kind of
    conversion), plus the dual-source traceability App-Flow §3.4 requires:
    which candidate ideas from `external_knowledge` this drew on, and which
    tested `knowledge_entries` it respected or knowingly overrode. There is
    no `change_summary` here — nothing preceded this to summarise a change
    from. `extra="forbid"` for the same reason as its siblings: an invented
    field must fail loudly, not be silently dropped and quietly change what
    got proposed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    family: str
    market: str
    timeframe: str
    entry_logic: list[Node] = Field(default_factory=list)
    exit_logic: list[Node] = Field(default_factory=list)
    filter_logic: list[Node] = Field(default_factory=list)
    risk_logic: list[Node] = Field(default_factory=list)
    universe: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    hypothesis: str
    rationale: str | None = None
    expected_behavior: str | None = None
    source_external_knowledge_ids: list[int] = Field(default_factory=list)
    source_internal_knowledge_ids: list[int] = Field(default_factory=list)

    def to_spec(self) -> StrategySpec:
        return StrategySpec(
            entry_logic=self.entry_logic,
            exit_logic=self.exit_logic,
            filter_logic=self.filter_logic,
            risk_logic=self.risk_logic,
            universe=self.universe,
            parameters=self.parameters,
            hypothesis=self.hypothesis,
            rationale=self.rationale,
            expected_behavior=self.expected_behavior,
        )


class ProposedPromotion(BaseModel):
    """A4's one output shape — a recommendation, never an action (App-Flow
    §7, Backend-Schema §7's `promotions` table).

    Mirrors `promotions` field-for-field, with two deliberate omissions.
    **No `requires_human_approval` field** — that column is hardcoded to 1
    by `handlers/promote.py` regardless of `decision`, so an `approve`
    verdict can never be self-certifying (App-Flow §7.3: *"A4 can say no
    alone, never yes alone"*). **No portfolio-correlation field** — out of
    scope for v1 and computed independently by the dashboard, never fed into
    A4's decision (App-Flow §7.2). `extra="forbid"` for the same reason as
    every sibling shape here: an invented field must fail loudly.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "defer"]
    rationale: str
    evidence_summary: dict[str, Any] = Field(default_factory=dict)
    # The overfitting signal App-Flow §7.1 requires A4 to weigh explicitly —
    # iteration count alone is a number; this is A4's judgement of it.
    overfitting_risk: Literal["low", "medium", "high"]
    confidence: float | None = None
    # Can THIS strategy alone trade at real size — a single-strategy
    # property (App-Flow §7's capacity/liquidity assessment).
    capacity_liquidity_ok: bool
    recommended_allocation_pct: float | None = None


class LabNotebookDraft(BaseModel):
    """One `lab_notebooks` row (Backend-Schema §9, TRD §16). `next_questions`
    is **mandatory and non-empty** — the model must propose at least one; an
    empty list is what self-propagation failing to happen looks like."""

    model_config = ConfigDict(extra="forbid")

    hypothesis: str
    result: str
    reason: str
    evidence: str
    confidence: float | None = None
    next_questions: list[str] = Field(min_length=1)


class KnowledgeEntryDraft(BaseModel):
    """One `knowledge_entries` row (Backend-Schema §9). `future_ideas` is
    **mandatory and non-empty** for the same reason `next_questions` is —
    TRD §12.1: *"this is what self-propels the lab."* `documents_failure_reasons`
    is Stage 8's own addition beyond the schema's literal columns: which
    structured `experiments.failure_reason` value(s) this lesson documents,
    written into the stored row's `evidence` JSON by the handler rather than
    matched later against free-form `statement` prose — see
    `db.repositories.knowledge.repeat_failure_rate`."""

    model_config = ConfigDict(extra="forbid")

    entry_type: Literal["experiment_record", "lesson", "global_rule", "pattern"]
    scope: Literal["experiment", "family", "market", "global"]
    title: str
    statement: str
    evidence_experiment_ids: list[int] = Field(default_factory=list)
    documents_failure_reasons: list[ResearchFailureReason] = Field(default_factory=list)
    confidence: float | None = None
    applicable_markets: list[str] = Field(default_factory=list)
    applicable_timeframes: list[str] = Field(default_factory=list)
    applicable_regimes: list[str] = Field(default_factory=list)
    future_ideas: list[str] = Field(min_length=1)
    # The real `knowledge_entries.id` of an EXISTING row this one contradicts
    # and replaces, or None. Only ever a row the brief already showed A5 (its
    # id is visible there) — never a forward reference to a sibling entry in
    # this same response, which has no database id yet.
    supersedes_existing_id: int | None = None


class KnowledgeEdgeDraft(BaseModel):
    """One `knowledge_edges` observation (TRD §12.5). `evidence_experiment_ids`
    is **mandatory and non-empty** — *"an edge with no experiment backing
    must not exist"* is enforced again at the repository layer
    (`KnowledgeEdgeRepository.observe`), but requiring it here means a
    malformed response fails schema validation before it ever reaches that
    check."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    predicate: Literal["works_in", "fails_in", "pairs_well_with", "pairs_poorly_with", "requires", "degrades_with"]
    object: str
    supports: bool = True
    confidence: float | None = None
    evidence_experiment_ids: list[int] = Field(min_length=1)


class ResearchQuestionDraft(BaseModel):
    """One `research_questions` row pushed to the curiosity queue (TRD
    §12.4)."""

    model_config = ConfigDict(extra="forbid")

    question: str
    motivation: str | None = None
    priority: int = 0


class ProposedKnowledge(BaseModel):
    """A5's one output shape — serves both jobs it runs (App-Flow §8):
    `ARCHIVE` (per-strategy, `notebook` required) and `MINE_PATTERNS`
    (cross-experiment, `notebook` always `None` — there is no single
    strategy's story to narrate). The handler enforces which is required
    for which job; this schema only says what each field means.
    """

    model_config = ConfigDict(extra="forbid")

    notebook: LabNotebookDraft | None = None
    entries: list[KnowledgeEntryDraft] = Field(default_factory=list)
    edges: list[KnowledgeEdgeDraft] = Field(default_factory=list)
    questions: list[ResearchQuestionDraft] = Field(default_factory=list)


@dataclass(frozen=True)
class AgentResponse:
    """What a session call returns — everything a caller needs to persist."""

    spec: ProposedSpec
    prompt_version: str
    tokens_spent: int
    raw_output: dict[str, Any]


@dataclass(frozen=True)
class ReviewResponse:
    """What a review call returns — the A3 counterpart to `AgentResponse`."""

    plan: ProposedPlan
    prompt_version: str
    tokens_spent: int
    raw_output: dict[str, Any]


@dataclass(frozen=True)
class HypothesisResponse:
    """What a generate call returns — the A1 counterpart to `AgentResponse`."""

    hypothesis: ProposedHypothesis
    prompt_version: str
    tokens_spent: int
    raw_output: dict[str, Any]


@dataclass(frozen=True)
class PromotionResponse:
    """What a promotion call returns — the A4 counterpart to `AgentResponse`."""

    promotion: ProposedPromotion
    prompt_version: str
    tokens_spent: int
    raw_output: dict[str, Any]


@dataclass(frozen=True)
class KnowledgeResponse:
    """What an archive/mine call returns — the A5 counterpart to `AgentResponse`."""

    knowledge: ProposedKnowledge
    prompt_version: str
    tokens_spent: int
    raw_output: dict[str, Any]


class AgentSession(Protocol):
    def propose_spec(self, brief: str, *, prompt_version: str) -> AgentResponse:
        """Turn one complete brief into one `ProposedSpec`. Stateless."""
        ...


class ReviewSession(Protocol):
    def review(self, brief: str, *, prompt_version: str) -> ReviewResponse:
        """Turn one complete brief into one `ProposedPlan`. Stateless."""
        ...


class HypothesisSession(Protocol):
    def generate(self, brief: str, *, prompt_version: str) -> HypothesisResponse:
        """Turn one complete Research Brief into one `ProposedHypothesis`. Stateless."""
        ...


class PromotionSession(Protocol):
    def decide(self, brief: str, *, prompt_version: str) -> PromotionResponse:
        """Turn one complete Promotion Brief into one `ProposedPromotion`. Stateless."""
        ...


class KnowledgeSession(Protocol):
    def archive(self, brief: str, *, prompt_version: str) -> KnowledgeResponse:
        """Turn one complete Archive/Mining Brief into one `ProposedKnowledge`. Stateless."""
        ...


class StubSession:
    """Returns a fixed (or brief-derived) `ProposedSpec`. No network, ever.

    The right choice whenever a test needs to exercise the iteration/FIX_CODE
    path without caring what a real model would say — the fixed response is
    the point, not a stand-in for one.
    """

    def __init__(self, response: ProposedSpec | Callable[[str], ProposedSpec]) -> None:
        self._response = response
        self.calls: list[str] = []

    def propose_spec(self, brief: str, *, prompt_version: str) -> AgentResponse:
        self.calls.append(brief)
        spec = self._response(brief) if callable(self._response) else self._response
        return AgentResponse(
            spec=spec, prompt_version=prompt_version, tokens_spent=0, raw_output=spec.model_dump(mode="json")
        )


class ReplaySession:
    """Replays pre-recorded responses in order — one per call.

    For tests that exercise more than one LLM round-trip (e.g. a `FIX_CODE`
    retry after a deliberately bad first attempt) and want each response to
    differ deterministically, without either a live call or a single fixed
    stub answering every call identically.
    """

    def __init__(self, fixtures: Sequence[dict[str, Any]]) -> None:
        self._fixtures = list(fixtures)
        self._index = 0

    def propose_spec(self, brief: str, *, prompt_version: str) -> AgentResponse:
        if self._index >= len(self._fixtures):
            raise RuntimeError(
                f"ReplaySession exhausted after {self._index} call(s): no more recorded responses"
            )
        raw = self._fixtures[self._index]
        self._index += 1
        spec = ProposedSpec(**raw)
        return AgentResponse(spec=spec, prompt_version=prompt_version, tokens_spent=0, raw_output=raw)


class StubReviewSession:
    """`StubSession`'s A3 counterpart. Returns a fixed (or brief-derived)
    `ProposedPlan`. No network, ever."""

    def __init__(self, response: ProposedPlan | Callable[[str], ProposedPlan]) -> None:
        self._response = response
        self.calls: list[str] = []

    def review(self, brief: str, *, prompt_version: str) -> ReviewResponse:
        self.calls.append(brief)
        plan = self._response(brief) if callable(self._response) else self._response
        return ReviewResponse(
            plan=plan, prompt_version=prompt_version, tokens_spent=0, raw_output=plan.model_dump(mode="json")
        )


class ReplayReviewSession:
    """`ReplaySession`'s A3 counterpart — replays pre-recorded plans in order,
    one per call. Used by the closed-loop test to make each successive A3
    verdict differ deterministically (e.g. `iterate` four times, then
    `plateau` on the fifth) without a live call."""

    def __init__(self, fixtures: Sequence[dict[str, Any]]) -> None:
        self._fixtures = list(fixtures)
        self._index = 0

    def review(self, brief: str, *, prompt_version: str) -> ReviewResponse:
        if self._index >= len(self._fixtures):
            raise RuntimeError(
                f"ReplayReviewSession exhausted after {self._index} call(s): no more recorded responses"
            )
        raw = self._fixtures[self._index]
        self._index += 1
        plan = ProposedPlan(**raw)
        return ReviewResponse(plan=plan, prompt_version=prompt_version, tokens_spent=0, raw_output=raw)


class StubHypothesisSession:
    """`StubSession`'s A1 counterpart. Returns a fixed (or brief-derived)
    `ProposedHypothesis`. No network, ever."""

    def __init__(self, response: ProposedHypothesis | Callable[[str], ProposedHypothesis]) -> None:
        self._response = response
        self.calls: list[str] = []

    def generate(self, brief: str, *, prompt_version: str) -> HypothesisResponse:
        self.calls.append(brief)
        hypothesis = self._response(brief) if callable(self._response) else self._response
        return HypothesisResponse(
            hypothesis=hypothesis,
            prompt_version=prompt_version,
            tokens_spent=0,
            raw_output=hypothesis.model_dump(mode="json"),
        )


class ReplayHypothesisSession:
    """`ReplaySession`'s A1 counterpart — replays pre-recorded hypotheses in
    order, one per call. For tests that drive more than one `GENERATE_SPEC`
    call (e.g. an exact-duplicate rejection followed by a novel retry) and
    want each response to differ deterministically."""

    def __init__(self, fixtures: Sequence[dict[str, Any]]) -> None:
        self._fixtures = list(fixtures)
        self._index = 0

    def generate(self, brief: str, *, prompt_version: str) -> HypothesisResponse:
        if self._index >= len(self._fixtures):
            raise RuntimeError(
                f"ReplayHypothesisSession exhausted after {self._index} call(s): no more recorded responses"
            )
        raw = self._fixtures[self._index]
        self._index += 1
        hypothesis = ProposedHypothesis(**raw)
        return HypothesisResponse(hypothesis=hypothesis, prompt_version=prompt_version, tokens_spent=0, raw_output=raw)


class StubPromotionSession:
    """`StubSession`'s A4 counterpart. Returns a fixed (or brief-derived)
    `ProposedPromotion`. No network, ever."""

    def __init__(self, response: ProposedPromotion | Callable[[str], ProposedPromotion]) -> None:
        self._response = response
        self.calls: list[str] = []

    def decide(self, brief: str, *, prompt_version: str) -> PromotionResponse:
        self.calls.append(brief)
        promotion = self._response(brief) if callable(self._response) else self._response
        return PromotionResponse(
            promotion=promotion,
            prompt_version=prompt_version,
            tokens_spent=0,
            raw_output=promotion.model_dump(mode="json"),
        )


class ReplayPromotionSession:
    """`ReplaySession`'s A4 counterpart — replays pre-recorded promotions in
    order, one per call."""

    def __init__(self, fixtures: Sequence[dict[str, Any]]) -> None:
        self._fixtures = list(fixtures)
        self._index = 0

    def decide(self, brief: str, *, prompt_version: str) -> PromotionResponse:
        if self._index >= len(self._fixtures):
            raise RuntimeError(
                f"ReplayPromotionSession exhausted after {self._index} call(s): no more recorded responses"
            )
        raw = self._fixtures[self._index]
        self._index += 1
        promotion = ProposedPromotion(**raw)
        return PromotionResponse(promotion=promotion, prompt_version=prompt_version, tokens_spent=0, raw_output=raw)


class StubKnowledgeSession:
    """`StubSession`'s A5 counterpart. Returns a fixed (or brief-derived)
    `ProposedKnowledge`. No network, ever."""

    def __init__(self, response: ProposedKnowledge | Callable[[str], ProposedKnowledge]) -> None:
        self._response = response
        self.calls: list[str] = []

    def archive(self, brief: str, *, prompt_version: str) -> KnowledgeResponse:
        self.calls.append(brief)
        knowledge = self._response(brief) if callable(self._response) else self._response
        return KnowledgeResponse(
            knowledge=knowledge,
            prompt_version=prompt_version,
            tokens_spent=0,
            raw_output=knowledge.model_dump(mode="json"),
        )


class ReplayKnowledgeSession:
    """`ReplaySession`'s A5 counterpart — replays pre-recorded knowledge
    outputs in order, one per call (`ARCHIVE` and `MINE_PATTERNS` are
    separate jobs, so a test driving both installs two fixtures)."""

    def __init__(self, fixtures: Sequence[dict[str, Any]]) -> None:
        self._fixtures = list(fixtures)
        self._index = 0

    def archive(self, brief: str, *, prompt_version: str) -> KnowledgeResponse:
        if self._index >= len(self._fixtures):
            raise RuntimeError(
                f"ReplayKnowledgeSession exhausted after {self._index} call(s): no more recorded responses"
            )
        raw = self._fixtures[self._index]
        self._index += 1
        knowledge = ProposedKnowledge(**raw)
        return KnowledgeResponse(knowledge=knowledge, prompt_version=prompt_version, tokens_spent=0, raw_output=raw)


class AnthropicSession:
    """The real wrapper — one stateless call per `propose_spec`/`review`/
    `generate` call (TRD §16).

    Imports `anthropic` lazily so the rest of this codebase, including every
    test that never constructs this class, has no hard dependency on the
    package or a configured API key.
    """

    def __init__(self, *, model: str | None = None, max_tokens: int = 8000, client: Any = None) -> None:
        self.model = model or get_settings().anthropic_model
        self.max_tokens = max_tokens
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import anthropic

        self._client = anthropic.Anthropic()
        return self._client

    def _parse(self, brief: str, *, output_format: type[BaseModel]) -> tuple[BaseModel, int]:
        client = self._get_client()
        response = client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": brief}],
            output_format=output_format,
        )
        tokens_spent = int(response.usage.input_tokens) + int(response.usage.output_tokens)
        return response.parsed_output, tokens_spent

    def propose_spec(self, brief: str, *, prompt_version: str) -> AgentResponse:
        spec, tokens_spent = self._parse(brief, output_format=ProposedSpec)
        return AgentResponse(
            spec=spec,
            prompt_version=prompt_version,
            tokens_spent=tokens_spent,
            raw_output=spec.model_dump(mode="json"),
        )

    def review(self, brief: str, *, prompt_version: str) -> ReviewResponse:
        plan, tokens_spent = self._parse(brief, output_format=ProposedPlan)
        return ReviewResponse(
            plan=plan,
            prompt_version=prompt_version,
            tokens_spent=tokens_spent,
            raw_output=plan.model_dump(mode="json"),
        )

    def generate(self, brief: str, *, prompt_version: str) -> HypothesisResponse:
        hypothesis, tokens_spent = self._parse(brief, output_format=ProposedHypothesis)
        return HypothesisResponse(
            hypothesis=hypothesis,
            prompt_version=prompt_version,
            tokens_spent=tokens_spent,
            raw_output=hypothesis.model_dump(mode="json"),
        )

    def decide(self, brief: str, *, prompt_version: str) -> PromotionResponse:
        promotion, tokens_spent = self._parse(brief, output_format=ProposedPromotion)
        return PromotionResponse(
            promotion=promotion,
            prompt_version=prompt_version,
            tokens_spent=tokens_spent,
            raw_output=promotion.model_dump(mode="json"),
        )

    def archive(self, brief: str, *, prompt_version: str) -> KnowledgeResponse:
        knowledge, tokens_spent = self._parse(brief, output_format=ProposedKnowledge)
        return KnowledgeResponse(
            knowledge=knowledge,
            prompt_version=prompt_version,
            tokens_spent=tokens_spent,
            raw_output=knowledge.model_dump(mode="json"),
        )

"""The Claude session wrapper (TRD §16) — the non-network implementations.

`AnthropicSession` is exercised only for its lazy import (never constructing
a real client here — that would need `ANTHROPIC_API_KEY`); everything about
its actual behavior is covered by the handler tests via a `StubSession`.
"""
from __future__ import annotations

import pytest

from aqrl.agents.session import (
    AnthropicSession,
    KnowledgeEdgeDraft,
    KnowledgeEntryDraft,
    LabNotebookDraft,
    ProposedKnowledge,
    ProposedPromotion,
    ProposedSpec,
    ReplayKnowledgeSession,
    ReplayPromotionSession,
    ReplaySession,
    StubKnowledgeSession,
    StubPromotionSession,
    StubSession,
)


def _spec(hypothesis: str = "h", change_summary: str = "cs") -> ProposedSpec:
    return ProposedSpec(
        entry_logic=[{"id": "e", "operator": "ema", "params": {"span": 5}, "inputs": {"series": "price.close"}}],
        hypothesis=hypothesis,
        change_summary=change_summary,
    )


def test_proposed_spec_to_spec_round_trips_the_dag():
    proposed = _spec()
    spec = proposed.to_spec()
    assert spec.hypothesis == "h"
    assert spec.entry_logic[0].operator == "ema"


def test_proposed_spec_rejects_unknown_fields():
    with pytest.raises(Exception):
        ProposedSpec(hypothesis="h", change_summary="cs", not_a_real_field=1)


def test_stub_session_returns_fixed_response_and_records_calls():
    spec = _spec()
    stub = StubSession(spec)
    response = stub.propose_spec("brief text", prompt_version="v1")
    assert response.spec is spec
    assert response.prompt_version == "v1"
    assert response.tokens_spent == 0
    assert stub.calls == ["brief text"]


def test_stub_session_accepts_a_callable():
    calls = []

    def make(brief: str) -> ProposedSpec:
        calls.append(brief)
        return _spec(hypothesis=f"from:{brief}")

    stub = StubSession(make)
    response = stub.propose_spec("hello", prompt_version="v1")
    assert response.spec.hypothesis == "from:hello"
    assert calls == ["hello"]


def test_replay_session_returns_fixtures_in_order():
    replay = ReplaySession(
        [
            {"hypothesis": "first", "change_summary": "cs1"},
            {"hypothesis": "second", "change_summary": "cs2"},
        ]
    )
    first = replay.propose_spec("brief", prompt_version="v1")
    second = replay.propose_spec("brief", prompt_version="v1")
    assert first.spec.hypothesis == "first"
    assert second.spec.hypothesis == "second"


def test_replay_session_raises_when_exhausted():
    replay = ReplaySession([{"hypothesis": "only", "change_summary": "cs"}])
    replay.propose_spec("brief", prompt_version="v1")
    with pytest.raises(RuntimeError, match="exhausted"):
        replay.propose_spec("brief", prompt_version="v1")


def test_anthropic_session_does_not_require_a_key_until_called():
    # Constructing the wrapper must not touch the network or require a key —
    # only calling propose_spec() does, and this test deliberately never does.
    session = AnthropicSession(model="claude-opus-5")
    assert session.model == "claude-opus-5"


# -- ProposedPromotion (A4, Stage 8) -------------------------------------------


def _promotion(decision: str = "approve") -> ProposedPromotion:
    return ProposedPromotion(
        decision=decision, rationale="r", overfitting_risk="low", capacity_liquidity_ok=True
    )


def test_proposed_promotion_rejects_unknown_fields():
    with pytest.raises(Exception):
        ProposedPromotion(
            decision="approve", rationale="r", overfitting_risk="low", capacity_liquidity_ok=True,
            requires_human_approval=True,
        )


def test_proposed_promotion_has_no_portfolio_correlation_field():
    """App-Flow §7.2 — out of scope for v1, never part of A4's brief or
    output."""
    assert "portfolio_correlation" not in ProposedPromotion.model_fields
    assert "correlation" not in ProposedPromotion.model_fields


def test_stub_promotion_session_returns_fixed_response():
    promotion = _promotion()
    stub = StubPromotionSession(promotion)
    response = stub.decide("brief", prompt_version="v1")
    assert response.promotion is promotion
    assert response.tokens_spent == 0
    assert stub.calls == ["brief"]


def test_replay_promotion_session_returns_fixtures_in_order():
    replay = ReplayPromotionSession(
        [
            {"decision": "approve", "rationale": "r1", "overfitting_risk": "low", "capacity_liquidity_ok": True},
            {"decision": "reject", "rationale": "r2", "overfitting_risk": "high", "capacity_liquidity_ok": False},
        ]
    )
    first = replay.decide("brief", prompt_version="v1")
    second = replay.decide("brief", prompt_version="v1")
    assert first.promotion.decision == "approve"
    assert second.promotion.decision == "reject"


def test_replay_promotion_session_raises_when_exhausted():
    replay = ReplayPromotionSession(
        [{"decision": "defer", "rationale": "r", "overfitting_risk": "medium", "capacity_liquidity_ok": True}]
    )
    replay.decide("brief", prompt_version="v1")
    with pytest.raises(RuntimeError, match="exhausted"):
        replay.decide("brief", prompt_version="v1")


# -- ProposedKnowledge (A5, Stage 8) -------------------------------------------


def _knowledge() -> ProposedKnowledge:
    return ProposedKnowledge(
        notebook=LabNotebookDraft(
            hypothesis="h", result="r", reason="re", evidence="e", next_questions=["q"]
        ),
        entries=[
            KnowledgeEntryDraft(entry_type="lesson", scope="global", title="t", statement="s", future_ideas=["idea"])
        ],
        edges=[KnowledgeEdgeDraft(subject="a", predicate="fails_in", object="b", evidence_experiment_ids=[1])],
    )


def test_lab_notebook_draft_requires_non_empty_next_questions():
    with pytest.raises(Exception):
        LabNotebookDraft(hypothesis="h", result="r", reason="re", evidence="e", next_questions=[])


def test_knowledge_entry_draft_requires_non_empty_future_ideas():
    with pytest.raises(Exception):
        KnowledgeEntryDraft(entry_type="lesson", scope="global", title="t", statement="s", future_ideas=[])


def test_knowledge_edge_draft_requires_non_empty_evidence_experiment_ids():
    with pytest.raises(Exception):
        KnowledgeEdgeDraft(subject="a", predicate="fails_in", object="b", evidence_experiment_ids=[])


def test_proposed_knowledge_notebook_defaults_to_none_for_mine_patterns():
    knowledge = ProposedKnowledge(entries=[], edges=[], questions=[])
    assert knowledge.notebook is None


def test_stub_knowledge_session_returns_fixed_response():
    knowledge = _knowledge()
    stub = StubKnowledgeSession(knowledge)
    response = stub.archive("brief", prompt_version="v1")
    assert response.knowledge is knowledge
    assert response.tokens_spent == 0
    assert stub.calls == ["brief"]


def test_replay_knowledge_session_returns_fixtures_in_order():
    replay = ReplayKnowledgeSession(
        [
            {"entries": [], "edges": [], "questions": []},
            {
                "notebook": {
                    "hypothesis": "h", "result": "r", "reason": "re", "evidence": "e", "next_questions": ["q"]
                },
                "entries": [], "edges": [], "questions": [],
            },
        ]
    )
    first = replay.archive("brief", prompt_version="v1")
    second = replay.archive("brief", prompt_version="v1")
    assert first.knowledge.notebook is None
    assert second.knowledge.notebook.hypothesis == "h"


def test_replay_knowledge_session_raises_when_exhausted():
    replay = ReplayKnowledgeSession([{"entries": [], "edges": [], "questions": []}])
    replay.archive("brief", prompt_version="v1")
    with pytest.raises(RuntimeError, match="exhausted"):
        replay.archive("brief", prompt_version="v1")

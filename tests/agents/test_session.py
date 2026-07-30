"""The Claude session wrapper (TRD §16) — the non-network implementations.

`AnthropicSession` is exercised only for its lazy import (never constructing
a real client here — that would need `ANTHROPIC_API_KEY`); everything about
its actual behavior is covered by the handler tests via a `StubSession`.
"""
from __future__ import annotations

import pytest

from aqrl.agents.session import AnthropicSession, ProposedSpec, ReplaySession, StubSession


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

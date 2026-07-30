"""The Claude session wrapper — stateless, schema-validated (TRD §16).

**Stateless and disposable.** Every call is a fresh `AgentSession.propose_spec`
with a complete brief; nothing here remembers a previous call, matching TRD
§16's *"Model: Claude, via Claude Code sessions. Stateless and disposable."*
Context assembly is `context.py`'s job, never this module's — a session
wrapper that goes looking for its own context is exactly what TRD §16 rules
out (*"Claude does not go hunting for context"*).

**Structured outputs, not prose parsing.** `ProposedSpec` is the one shape
A2 may return: an operator DAG plus a plain-language `change_summary` — never
free code (App-Flow §4.2: *"translation, not invention"*). The real
implementation (`AnthropicSession`) uses `client.messages.parse(...,
output_format=ProposedSpec)`, so the SDK — not a hand-rolled JSON parser —
is what guarantees a malformed response never reaches the render/check
pipeline.

**Three implementations, one protocol.** `AnthropicSession` is the only one
that touches the network; `StubSession` and `ReplaySession` return
pre-supplied responses so every test in this codebase — including the full
offline loop test — never needs `ANTHROPIC_API_KEY` or a live connection.
Retries are the SDK's own (`max_retries`, default 2, on 429/5xx/connection
errors) plus `aqrl.orchestration.failures.classify`, which already lists
`RateLimitError`/`APITimeoutError`/`APIConnectionError` as transient — this
module does not duplicate that logic, it just lets those exceptions propagate
to the worker, exactly like any other handler failure.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..operators.spec import Node, StrategySpec

__all__ = [
    "AgentResponse",
    "AgentSession",
    "AnthropicSession",
    "ProposedSpec",
    "ReplaySession",
    "StubSession",
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


@dataclass(frozen=True)
class AgentResponse:
    """What a session call returns — everything a caller needs to persist."""

    spec: ProposedSpec
    prompt_version: str
    tokens_spent: int
    raw_output: dict[str, Any]


class AgentSession(Protocol):
    def propose_spec(self, brief: str, *, prompt_version: str) -> AgentResponse:
        """Turn one complete brief into one `ProposedSpec`. Stateless."""
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


class AnthropicSession:
    """The real wrapper — one stateless call per `propose_spec` (TRD §16).

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

    def propose_spec(self, brief: str, *, prompt_version: str) -> AgentResponse:
        client = self._get_client()
        response = client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": brief}],
            output_format=ProposedSpec,
        )
        spec = response.parsed_output
        tokens_spent = int(response.usage.input_tokens) + int(response.usage.output_tokens)
        return AgentResponse(
            spec=spec,
            prompt_version=prompt_version,
            tokens_spent=tokens_spent,
            raw_output=spec.model_dump(mode="json"),
        )

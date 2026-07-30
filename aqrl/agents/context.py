"""The Implementation Brief — assembled in Python (App-Flow §4.1).

**Python builds the brief; Claude does not go hunting for context** (TRD
§16). Every field the brief can contain is named here explicitly:

    strategy identity (name, family, market, timeframe)
    operator catalog (name, category, params, valid markets/timeframes)
    the spec being iterated on, if any
    the research plan driving this iteration, if any (App-Flow §6.4: never code)
    the prior code version's diff, if any
    the prior evaluation's summary, if any — outcome and phase, never internals
    static-check diagnostics, if this is a FIX_CODE retry

**What never appears here, and why.** No `evaluate.py` source, no honest-score
formula or thresholds, no bar values, no vault data. App-Flow §4.1 is explicit
that *"A2 never receives `evaluate.py`"* — an agent that can see how it will
be scored eventually optimises for the scorer instead of the market. A stored
*outcome* (pass/fail, which phase, which named failure reason) is a fact about
what already happened, not a description of the scoring function, so it is
safe to include; the function that produced it is not.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..db.repositories.base import Row
from ..hashing import content_hash
from ..operators.registry import all_operators
from ..operators.spec import StrategySpec

__all__ = ["assemble_implement_brief", "implement_prompt_version"]

_PROMPT_PATH = Path(__file__).parent / "prompts" / "implement_v1.md"


def _template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def implement_prompt_version() -> str:
    """Derived from the template's content, never hand-bumped.

    Same reasoning as `operator_library_version` (`aqrl/operators/registry.py`):
    a prompt version that must be remembered is a prompt version that will be
    forgotten the first time the wording changes, and every output recorded
    under the stale version silently becomes incomparable noise.
    """
    return f"implement-v1-{content_hash(_template())[:12]}"


def _operator_catalog() -> list[dict[str, Any]]:
    """Name, category, params, applicability — never `implementation_ref`
    (the Python module path), which is an implementation detail A2 has no
    use for and no business depending on."""
    return [
        {
            "name": op.name,
            "version": op.version,
            "category": op.category,
            "description": op.description,
            "inputs": list(op.inputs),
            "params": [spec.descriptor() for spec in op.params],
            "valid_markets": sorted(op.valid_markets) if op.valid_markets else None,
            "valid_timeframes": sorted(op.valid_timeframes) if op.valid_timeframes else None,
            "commutative": op.commutative,
        }
        for op in all_operators()
    ]


def _spec_section(spec: StrategySpec | None) -> str:
    if spec is None:
        return "(no prior spec — this is the strategy's first iteration)"
    payload = spec.model_dump(mode="json")
    return json.dumps(payload, indent=2, sort_keys=True)


def _plan_section(research_plan: Row | None) -> str:
    if research_plan is None:
        return "(none — this is not an iteration in response to a research plan)"
    fields = {
        "verdict": research_plan.get("verdict"),
        "diagnosis": research_plan.get("diagnosis"),
        "evidence_cited": research_plan.get("evidence_cited"),
        "proposed_changes": research_plan.get("proposed_changes"),
        "expected_effect": research_plan.get("expected_effect"),
        "confidence": research_plan.get("confidence"),
    }
    return json.dumps(fields, indent=2, sort_keys=True, default=str)


def _prior_code_section(prior_diff: str | None) -> str:
    if not prior_diff:
        return "(no prior code version, or nothing changed since it)"
    return prior_diff


def _prior_evaluation_section(prior_evaluation: Row | None) -> str:
    """Outcome facts only — never the engine that produced them (module
    docstring). `metrics_json` and every individual metric are deliberately
    excluded even though the row carries them: those values are close enough
    to the scoring surface that including them risks the same
    aiming-at-the-scorer failure App-Flow §4.1 warns about. `phase` /
    `result` / `bar_failed_on` are the durable, ungameable facts — *which
    stage stopped it, and on what dimension* — without exposing the metric
    values or thresholds that decided it."""
    if prior_evaluation is None:
        return "(no prior evaluation)"
    fields = {
        "phase": prior_evaluation.get("phase"),
        "result": prior_evaluation.get("result"),
        "bar_failed_on": prior_evaluation.get("bar_failed_on"),
    }
    return json.dumps(fields, indent=2, sort_keys=True, default=str)


def _diagnostics_section(diagnostics: list[str] | None) -> str:
    if not diagnostics:
        return "(none — this is not a FIX_CODE retry)"
    return "\n".join(f"- {d}" for d in diagnostics)


def assemble_implement_brief(
    *,
    strategy: Row,
    prior_spec: StrategySpec | None,
    research_plan: Row | None,
    prior_diff: str | None,
    prior_evaluation: Row | None,
    diagnostics: list[str] | None = None,
) -> str:
    """Build the complete brief `AgentSession.propose_spec` receives.

    Stable content (the template, the operator catalog) comes first and the
    volatile, per-call content (this strategy's spec, plan, diagnostics)
    last — the same ordering `shared/prompt-caching.md` recommends, so a
    future move to a real cached, multi-turn session does not require
    reshuffling this function.
    """
    sections = [
        _template(),
        "## Strategy",
        json.dumps(
            {
                "name": strategy.get("name"),
                "family": strategy.get("family"),
                "market": strategy.get("market"),
                "timeframe": strategy.get("timeframe"),
            },
            indent=2,
            sort_keys=True,
        ),
        "## Operator catalog",
        json.dumps(_operator_catalog(), indent=2, sort_keys=True),
        "## Prior spec (if iterating)",
        _spec_section(prior_spec),
        "## Research plan (if this iteration responds to one)",
        _plan_section(research_plan),
        "## Prior code diff (if any)",
        _prior_code_section(prior_diff),
        "## Prior evaluation outcome (if any)",
        _prior_evaluation_section(prior_evaluation),
        "## Static-check diagnostics (if this is a FIX_CODE retry)",
        _diagnostics_section(diagnostics),
    ]
    return "\n\n".join(sections)

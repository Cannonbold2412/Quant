"""The Implementation Brief and the Review Brief — both assembled in Python
(App-Flow §4.1, §6.2).

**Python builds the brief; Claude does not go hunting for context** (TRD
§16). Every field the Implementation Brief can contain is named here
explicitly:

    strategy identity (name, family, market, timeframe)
    operator catalog (name, category, params, valid markets/timeframes)
    the spec being iterated on, if any
    the research plan driving this iteration, if any (App-Flow §6.4: never code)
    the prior code version's diff, if any
    the prior evaluation's summary, if any — outcome and phase, never internals
    static-check diagnostics, if this is a FIX_CODE retry

**What never appears in the Implementation Brief, and why.** No
`evaluate.py` source, no honest-score formula or thresholds, no bar values,
no vault data. App-Flow §4.1 is explicit that *"A2 never receives
`evaluate.py`"* — an agent that can see how it will be scored eventually
optimises for the scorer instead of the market. A stored *outcome* (pass/fail,
which phase, which named failure reason) is a fact about what already
happened, not a description of the scoring function, so it is safe to
include; the function that produced it is not.

**The Review Brief's redaction is deliberately asymmetric, not looser by
accident.** A3 only ever reviews a *bar failure* (App-Flow §6.1) — and a
bar failure has no `honest_score` at all (TRD §7.5), so there is no scoring
surface to protect A3 from the way there is for A2. What A3 needs instead is
exactly what would otherwise be withheld: `bar_failed_on` plus the raw
diagnostic values and thresholds behind it (how far below `min_trades`, how
far over `max_drawdown`) — App-Flow §6.3 is explicit that this is the *only*
signal available to tell "getting closer" from "stuck" without a synthetic
score for failing attempts. What stays hidden is unchanged either way: no
`evaluate.py` source, no honest-score formula, no bar thresholds beyond the
one this evaluation actually hit.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..db.repositories.base import Row
from ..hashing import content_hash
from ..operators.registry import all_operators
from ..operators.spec import StrategySpec

__all__ = [
    "assemble_implement_brief",
    "assemble_review_brief",
    "implement_prompt_version",
    "review_prompt_version",
]

_IMPLEMENT_PROMPT_PATH = Path(__file__).parent / "prompts" / "implement_v1.md"
_REVIEW_PROMPT_PATH = Path(__file__).parent / "prompts" / "review_v1.md"


def _template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def implement_prompt_version() -> str:
    """Derived from the template's content, never hand-bumped.

    Same reasoning as `operator_library_version` (`aqrl/operators/registry.py`):
    a prompt version that must be remembered is a prompt version that will be
    forgotten the first time the wording changes, and every output recorded
    under the stale version silently becomes incomparable noise.
    """
    return f"implement-v1-{content_hash(_template(_IMPLEMENT_PROMPT_PATH))[:12]}"


def review_prompt_version() -> str:
    """`implement_prompt_version`'s A3 counterpart — same derivation."""
    return f"review-v1-{content_hash(_template(_REVIEW_PROMPT_PATH))[:12]}"


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
        _template(_IMPLEMENT_PROMPT_PATH),
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


# -- the Review Brief (A3, Stage 6) -------------------------------------------


def _iteration_history_section(experiments: list[Row]) -> str:
    """Every experiment this strategy has run, oldest first — App-Flow §6.2's
    *"all prior experiments for this strategy (full history)"*. Provenance
    and metric fields are omitted for the same reason `_prior_evaluation_section`
    omits them from the Implementation Brief: they sit on `experiments`, not
    this table's business to re-expose."""
    if not experiments:
        return "(no prior experiments — this is the first iteration)"
    rows = [
        {
            "iteration": experiment.get("iteration"),
            "status": experiment.get("status"),
            "outcome": experiment.get("outcome"),
            "failure_reason": experiment.get("failure_reason"),
        }
        for experiment in experiments
    ]
    return json.dumps(rows, indent=2, sort_keys=True, default=str)


def _bar_failure_section(evaluation: Row, diagnostic_checks: list[Row]) -> str:
    """The one place A3 sees raw metric values — a bar failure has no
    `honest_score` to reason over instead (module docstring)."""
    checks = [
        {
            "test_name": check.get("test_name"),
            "category": check.get("category"),
            "result": check.get("result"),
            "value": check.get("value"),
            "threshold": check.get("threshold"),
            "detail": check.get("detail"),
        }
        for check in diagnostic_checks
    ]
    fields = {
        "bar_failed_on": evaluation.get("bar_failed_on"),
        "checks": checks,
    }
    return json.dumps(fields, indent=2, sort_keys=True, default=str)


def _regime_section(regime_rows: list[Row]) -> str:
    if not regime_rows:
        return "(no regime breakdown recorded for this evaluation)"
    rows = [
        {
            "regime": row.get("regime"),
            "sharpe": row.get("sharpe"),
            "cagr": row.get("cagr"),
            "max_drawdown": row.get("max_drawdown"),
            "trade_count": row.get("trade_count"),
        }
        for row in regime_rows
    ]
    return json.dumps(rows, indent=2, sort_keys=True, default=str)


def _knowledge_section(knowledge_entries: list[Row]) -> str:
    if not knowledge_entries:
        return "(no related knowledge entries — none recorded yet, or none matched)"
    rows = [
        {
            "title": entry.get("title"),
            "statement": entry.get("statement"),
            "confidence": entry.get("confidence"),
        }
        for entry in knowledge_entries
    ]
    return json.dumps(rows, indent=2, sort_keys=True, default=str)


def _budget_section(*, iteration_count: int, plateau_counter: int, hard_iteration_cap: int, plateau_patience: int) -> str:
    fields = {
        "iteration_count": iteration_count,
        "hard_iteration_cap": hard_iteration_cap,
        "consecutive_bar_failures": plateau_counter,
        "plateau_patience": plateau_patience,
    }
    return json.dumps(fields, indent=2, sort_keys=True)


def assemble_review_brief(
    *,
    strategy: Row,
    spec: StrategySpec,
    experiment_history: list[Row],
    evaluation: Row,
    diagnostic_checks: list[Row],
    regime_performance: list[Row],
    knowledge_entries: list[Row],
    iteration_count: int,
    plateau_counter: int,
    hard_iteration_cap: int,
    plateau_patience: int,
) -> str:
    """Build the complete brief `ReviewSession.review` receives.

    Only ever called below the bar (App-Flow §6.1) — `evaluation` here is
    always a bar failure. Same stable-content-first ordering as
    `assemble_implement_brief`.
    """
    sections = [
        _template(_REVIEW_PROMPT_PATH),
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
        "## Spec under review",
        _spec_section(spec),
        "## Iteration history (full)",
        _iteration_history_section(experiment_history),
        "## This evaluation's bar failure and raw diagnostics",
        _bar_failure_section(evaluation, diagnostic_checks),
        "## Regime breakdown",
        _regime_section(regime_performance),
        "## Related knowledge entries",
        _knowledge_section(knowledge_entries),
        "## Remaining budget",
        _budget_section(
            iteration_count=iteration_count,
            plateau_counter=plateau_counter,
            hard_iteration_cap=hard_iteration_cap,
            plateau_patience=plateau_patience,
        ),
    ]
    return "\n\n".join(sections)

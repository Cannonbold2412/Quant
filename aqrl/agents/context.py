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
    "assemble_generate_brief",
    "assemble_implement_brief",
    "assemble_review_brief",
    "generate_prompt_version",
    "implement_prompt_version",
    "review_prompt_version",
]

_IMPLEMENT_PROMPT_PATH = Path(__file__).parent / "prompts" / "implement_v1.md"
_REVIEW_PROMPT_PATH = Path(__file__).parent / "prompts" / "review_v1.md"
_GENERATE_PROMPT_PATH = Path(__file__).parent / "prompts" / "generate_v1.md"


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


# -- the Research Brief (A1, Stage 7) -----------------------------------------


def generate_prompt_version() -> str:
    """`implement_prompt_version`'s A1 counterpart — same derivation."""
    return f"generate-v1-{content_hash(_template(_GENERATE_PROMPT_PATH))[:12]}"


def _goal_section(goal: Row) -> str:
    fields = {
        "title": goal.get("title"),
        "description": goal.get("description"),
        "market": goal.get("market"),
        "timeframe": goal.get("timeframe"),
        "allocation_bucket": goal.get("allocation_bucket"),
    }
    return json.dumps(fields, indent=2, sort_keys=True)


def _knowledge_rows_section(rows: list[Row], fields: tuple[str, ...]) -> str:
    if not rows:
        return "(none found)"
    projected = [{f: row.get(f) for f in ("id", *fields, "_relevance_score")} for row in rows]
    return json.dumps(projected, indent=2, sort_keys=True, default=str)


def _open_questions_section(questions: list[Row]) -> str:
    """`research_questions` (Backend-Schema §10) carries no `goal_id`
    column — it links to an originating experiment or knowledge entry, not
    a research goal. So "open questions for this goal" (App-Flow §3.2)
    cannot be scoped to *this* goal specifically; the brief surfaces every
    open question, priority-ordered, and leaves the relevance judgement to
    A1, the same way `_budget_section`-adjacent items elsewhere in this
    module surface raw facts rather than a pre-filtered subset the schema
    can't actually support."""
    if not questions:
        return "(no open research questions)"
    rows = [
        {"id": q.get("id"), "question": q.get("question"), "motivation": q.get("motivation"), "priority": q.get("priority")}
        for q in questions
    ]
    return json.dumps(rows, indent=2, sort_keys=True, default=str)


def _failure_patterns_section(entries: list[Row]) -> str:
    """The anti-amnesia surface (App-Flow §3.2/§3.4): internal lessons with
    counter-evidence or a `pattern`/`global_rule` entry type, scoped to this
    goal's market/timeframe. Surfaced whether or not A1 ends up touching
    them — `generate_v1.md`'s "must justify overriding" instruction has
    nothing to bite on if the contradicting lesson was never shown."""
    if not entries:
        return "(no recorded failure patterns for this market/timeframe)"
    rows = [
        {
            "id": entry.get("id"),
            "title": entry.get("title"),
            "statement": entry.get("statement"),
            "counter_evidence_count": entry.get("counter_evidence_count"),
            "confidence": entry.get("confidence"),
        }
        for entry in entries
    ]
    return json.dumps(rows, indent=2, sort_keys=True, default=str)


def _family_trials_section(family_counts: dict[str, int]) -> str:
    if not family_counts:
        return "(no strategies yet in this market/timeframe)"
    ordered = sorted(family_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    rows = [{"family": family, "trials": count} for family, count in ordered]
    return json.dumps(rows, indent=2, sort_keys=True)


def assemble_generate_brief(
    *,
    goal: Row,
    relevant_external_knowledge: list[Row],
    relevant_internal_knowledge: list[Row],
    open_questions: list[Row],
    failure_patterns: list[Row],
    family_trial_counts: dict[str, int],
) -> str:
    """Build the complete Research Brief `HypothesisSession.generate` receives.

    Stable content (the template, the operator catalog) first, volatile
    per-call content (this goal, its relevant knowledge) last — same ordering
    `assemble_implement_brief`/`assemble_review_brief` already use. Relevance
    search itself (`agents/research_brief.top_k_relevant`) is the caller's
    job, not this function's — this module only ever formats rows it is
    handed, matching how `assemble_review_brief` never queries the database
    on its own.
    """
    sections = [
        _template(_GENERATE_PROMPT_PATH),
        "## Research goal",
        _goal_section(goal),
        "## Operator catalog",
        json.dumps(_operator_catalog(), indent=2, sort_keys=True),
        "## Relevant external knowledge (candidate, UNTESTED)",
        _knowledge_rows_section(relevant_external_knowledge, ("core_idea", "category", "novelty_score")),
        "## Relevant internal knowledge (tested, TRUSTED)",
        _knowledge_rows_section(relevant_internal_knowledge, ("title", "statement", "confidence")),
        "## Open research questions",
        _open_questions_section(open_questions),
        "## Known failure patterns (anti-amnesia)",
        _failure_patterns_section(failure_patterns),
        "## Trials already spent, by family, in this market/timeframe",
        _family_trials_section(family_trial_counts),
    ]
    return "\n\n".join(sections)

"""The Implementation Brief (App-Flow §4.1) and the Review Brief (App-Flow
§6.2) — what each contains, and what each must never contain.
"""
from __future__ import annotations

from aqrl.agents.context import (
    archive_prompt_version,
    assemble_archive_brief,
    assemble_implement_brief,
    assemble_mine_brief,
    assemble_promote_brief,
    assemble_review_brief,
    implement_prompt_version,
    mine_prompt_version,
    promote_prompt_version,
    review_prompt_version,
)
from aqrl.operators.spec import Node, StrategySpec

_STRATEGY = {"name": "s", "family": "fam", "market": "nse_equity", "timeframe": "daily"}

#: Terms that would mean the evaluation engine's *scoring formula* leaked
#: into a brief A2 is not supposed to see (App-Flow §4.1). Note what is
#: deliberately absent from this list: `bar_failed_on` values like
#: `min_trades` / `max_drawdown` are *dimension names*, not numbers — the
#: same "dimensions, not thresholds" disclosure nanoAQRL's own `program.md`
#: makes on purpose ("The bar checks... a minimum trade count, a maximum
#: out-of-sample drawdown... The exact thresholds are... not disclosed").
_FORBIDDEN_TERMS = (
    "honest_score",
    "deflated_sharpe",
    "trials_haircut",
    "bar_verdict",
    "cost_stress_multiple",
    "evaluate_experiment",
    "walk_forward",
    "z_multiplier",
)


def _brief(**overrides):
    fields = dict(
        strategy=_STRATEGY,
        prior_spec=None,
        research_plan=None,
        prior_diff=None,
        prior_evaluation=None,
    )
    fields.update(overrides)
    return assemble_implement_brief(**fields)


def test_brief_never_leaks_evaluator_internals():
    brief = _brief(
        research_plan={
            "verdict": "iterate",
            "diagnosis": "no signal",
            "evidence_cited": {"note": "x"},
            "proposed_changes": [{"target": "entry", "change": "y", "reason": "z"}],
            "expected_effect": "fewer whipsaws",
            "confidence": 0.4,
        },
        prior_evaluation={"phase": "P2", "result": "fail", "bar_failed_on": "min_trades"},
        diagnostics=["spec_hash mismatch", "absolute price level on node e1"],
    )
    lowered = brief.lower()
    for term in _FORBIDDEN_TERMS:
        assert term not in lowered, f"forbidden term {term!r} leaked into the brief"


def test_prior_evaluation_section_carries_only_the_whitelisted_fields():
    """Structural guard, not just a substring check: even a *numeric* field
    this function has never heard of (a future column added to `evaluations`)
    must not pass through by accident — only `phase`/`result`/`bar_failed_on`
    are ever read out of the row (`context.py::_prior_evaluation_section`)."""
    row_with_everything = {
        "phase": "P2",
        "result": "fail",
        "bar_failed_on": "min_trades",
        "honest_score": 1.23,
        "sharpe": 0.9,
        "deflated_sharpe": 0.5,
        "metrics_json": {"sharpe": 0.9},
    }
    brief = _brief(prior_evaluation=row_with_everything)
    section = brief.split("## Prior evaluation outcome")[1].split("## Static-check diagnostics")[0]
    assert "1.23" not in section
    assert "0.9" not in section
    assert "sharpe" not in section.lower()
    assert "min_trades" in section  # bar_failed_on itself is whitelisted


def test_brief_includes_operator_catalog():
    brief = _brief()
    assert "ema" in brief
    assert "crossover" in brief
    assert "## Operator catalog" in brief


def test_brief_includes_strategy_identity():
    brief = _brief()
    assert "nse_equity" in brief
    assert "daily" in brief


def test_brief_marks_absent_sections_explicitly():
    brief = _brief()
    assert "no prior spec" in brief.lower()
    assert "this is not an iteration in response to a research plan" in brief.lower()
    assert "not a fix_code retry" in brief.lower()


def test_brief_includes_diagnostics_when_present():
    brief = _brief(diagnostics=["the spec hash did not match"])
    assert "the spec hash did not match" in brief


def test_prompt_version_is_stable_and_derived():
    a = implement_prompt_version()
    b = implement_prompt_version()
    assert a == b
    assert a.startswith("implement-v1-")


def test_prompt_version_changes_if_the_template_changes(tmp_path, monkeypatch):
    """Derived, never hand-bumped — the same property `operator_library_version`
    has (`aqrl/operators/registry.py`): editing the wording is a version change
    whether or not anyone remembers to say so."""
    import aqrl.agents.context as context_module

    before = implement_prompt_version()
    original = context_module._IMPLEMENT_PROMPT_PATH.read_text(encoding="utf-8")
    fake = tmp_path / "implement_v1.md"
    fake.write_text(original + "\nan appended line that changes the hash\n", encoding="utf-8")
    monkeypatch.setattr(context_module, "_IMPLEMENT_PROMPT_PATH", fake)
    assert implement_prompt_version() != before


# -- the Review Brief (A3, Stage 6) -------------------------------------------

_SPEC = StrategySpec(
    entry_logic=[
        Node(id="fast", operator="ema", inputs={"series": "price.close"}, params={"span": 10}),
        Node(id="slow", operator="ema", inputs={"series": "price.close"}, params={"span": 40}),
        Node(id="e1", operator="crossover", inputs={"fast": "fast", "slow": "slow"}),
    ],
    hypothesis="review-brief fixture: a plain dual-EMA crossover.",
)


def _review_brief(**overrides):
    fields = dict(
        strategy=_STRATEGY,
        spec=_SPEC,
        experiment_history=[],
        evaluation={"bar_failed_on": "min_trades"},
        diagnostic_checks=[],
        regime_performance=[],
        knowledge_entries=[],
        iteration_count=1,
        plateau_counter=1,
        hard_iteration_cap=25,
        plateau_patience=5,
    )
    fields.update(overrides)
    return assemble_review_brief(**fields)


def test_review_brief_includes_bar_failure_and_raw_diagnostics():
    """Unlike the Implementation Brief, A3 must see the raw metric values —
    a bar failure has no `honest_score` to reason over instead (App-Flow
    §6.3), so `bar_failed_on` plus the raw value/threshold pair is the only
    signal available."""
    brief = _review_brief(
        evaluation={"bar_failed_on": "min_trades"},
        diagnostic_checks=[
            {"test_name": "min_trades", "category": "performance", "result": "fail", "value": 42.0, "threshold": 100.0}
        ],
    )
    section = brief.split("## This evaluation's bar failure")[1].split("## Regime breakdown")[0]
    assert "min_trades" in section
    assert "42.0" in section
    assert "100.0" in section


def test_review_brief_includes_full_iteration_history():
    brief = _review_brief(
        experiment_history=[
            {"iteration": 1, "status": "evaluated", "outcome": "failed", "failure_reason": "no_signal"},
            {"iteration": 2, "status": "evaluated", "outcome": "failed", "failure_reason": "costs_exceed_edge"},
        ]
    )
    section = brief.split("## Iteration history")[1].split("## This evaluation's bar failure")[0]
    assert "no_signal" in section
    assert "costs_exceed_edge" in section
    assert '"iteration": 1' in section
    assert '"iteration": 2' in section


def test_review_brief_includes_regime_breakdown():
    brief = _review_brief(
        regime_performance=[
            {"regime": "trending", "sharpe": 0.9, "cagr": 0.1, "max_drawdown": 0.05, "trade_count": 20}
        ]
    )
    section = brief.split("## Regime breakdown")[1].split("## Related knowledge")[0]
    assert "trending" in section


def test_review_brief_includes_budget_and_iteration_counts():
    brief = _review_brief(iteration_count=7, plateau_counter=3, hard_iteration_cap=25, plateau_patience=5)
    section = brief.split("## Remaining budget")[1]
    assert '"iteration_count": 7' in section
    assert '"consecutive_bar_failures": 3' in section
    assert '"hard_iteration_cap": 25' in section
    assert '"plateau_patience": 5' in section


def test_review_brief_still_omits_engine_internals():
    """The redaction stays the same either way (module docstring) — only the
    *name* `honest_score` is exempt here, because the review prompt itself
    explains its structural absence below the bar (App-Flow §6.3: *"a
    bar-failing evaluation has no `honest_score` to compare"*). Explaining
    that a number does not exist is not the same as leaking it."""
    brief = _review_brief()
    lowered = brief.lower()
    for term in _FORBIDDEN_TERMS:
        if term == "honest_score":
            continue
        assert term not in lowered, f"forbidden term {term!r} leaked into the review brief"


def test_review_prompt_version_is_stable_and_derived():
    a = review_prompt_version()
    b = review_prompt_version()
    assert a == b
    assert a.startswith("review-v1-")


# -- the Promotion Brief (A4, Stage 8) -----------------------------------------

#: Unlike A2/A3, A4 may see the honest score and full metrics (module
#: docstring) — only the bar's own formula/threshold terms stay hidden.
_A4_FORBIDDEN_TERMS = ("z_multiplier", "trials_haircut", "cost_stress_multiple", "evaluate_experiment")


def _promote_brief(**overrides):
    fields = dict(
        strategy=_STRATEGY,
        spec=_SPEC,
        experiment_history=[],
        winning_evaluation={"honest_score": 0.9, "sharpe": 1.2, "bar_result": "pass"},
        diagnostic_checks=[],
        iteration_count=3,
    )
    fields.update(overrides)
    return assemble_promote_brief(**fields)


def test_promote_brief_may_include_the_honest_score():
    """The one deliberate asymmetry with every other brief in this module —
    A4 is a judge whose output never feeds a future spec (App-Flow §7)."""
    brief = _promote_brief(winning_evaluation={"honest_score": 0.87, "sharpe": 1.3, "bar_result": "pass"})
    section = brief.split("## Winning evaluation")[1].split("## Overfitting signal")[0]
    assert "0.87" in section
    assert "1.3" in section


def test_promote_brief_still_omits_the_bar_formula_and_thresholds():
    brief = _promote_brief()
    lowered = brief.lower()
    for term in _A4_FORBIDDEN_TERMS:
        assert term not in lowered, f"forbidden term {term!r} leaked into the promote brief"


def test_promote_brief_includes_full_iteration_history_including_bar_failures():
    brief = _promote_brief(
        experiment_history=[
            {"iteration": 1, "status": "reviewed", "outcome": "failed", "failure_reason": "insufficient_trades"},
            {"iteration": 2, "status": "evaluated", "outcome": "passed", "failure_reason": None},
        ]
    )
    section = brief.split("## Full iteration history")[1].split("## Winning evaluation")[0]
    assert "insufficient_trades" in section
    assert '"iteration": 1' in section
    assert '"iteration": 2' in section


def test_promote_brief_includes_the_overfitting_signal():
    brief = _promote_brief(iteration_count=31, winning_evaluation={"n_trials_used": 12, "bar_result": "pass"})
    section = brief.split("## Overfitting signal")[1].split("## Capacity")[0]
    assert '"iteration_count": 31' in section
    assert '"n_trials_used": 12' in section


def test_promote_brief_surfaces_capacity_checks_only():
    brief = _promote_brief(
        diagnostic_checks=[
            {"test_name": "equities_capacity", "result": "pass", "value": 0.02, "threshold": 0.1},
            {"test_name": "p1_no_signal", "result": "pass", "value": None, "threshold": None},
        ]
    )
    section = brief.split("## Capacity/liquidity evidence")[1]
    assert "equities_capacity" in section
    assert "p1_no_signal" not in section


def test_promote_prompt_version_is_stable_and_derived():
    a = promote_prompt_version()
    b = promote_prompt_version()
    assert a == b
    assert a.startswith("promote-v1-")


# -- the Archive / Mining Briefs (A5, Stage 8) ---------------------------------


def _archive_brief(**overrides):
    fields = dict(
        strategy=_STRATEGY,
        spec=_SPEC,
        experiment_history=[],
        evaluations=[],
        plans=[],
        promotion=None,
    )
    fields.update(overrides)
    return assemble_archive_brief(**fields)


def test_archive_brief_omits_engine_internals_including_honest_score():
    """A5's output reaches a future A1 brief (module docstring) — the same
    reward-hacking channel A3 is redacted against, so the full forbidden-term
    list applies here, `honest_score` included (unlike A4's brief)."""
    brief = _archive_brief(evaluations=[{"phase": "bar", "result": "fail", "bar_failed_on": "min_trades"}])
    lowered = brief.lower()
    for term in _FORBIDDEN_TERMS:
        assert term not in lowered, f"forbidden term {term!r} leaked into the archive brief"


def test_archive_brief_includes_all_iterations_and_evaluations():
    brief = _archive_brief(
        experiment_history=[{"iteration": 1, "status": "reviewed", "outcome": "failed", "failure_reason": "no_signal"}],
        evaluations=[{"phase": "bar", "result": "fail", "bar_failed_on": "min_trades"}],
    )
    history_section = brief.split("## All iterations")[1].split("## All evaluations")[0]
    assert "no_signal" in history_section
    eval_section = brief.split("## All evaluations")[1].split("## A3 research plans")[0]
    assert "min_trades" in eval_section


def test_archive_brief_marks_absent_promotion_explicitly():
    brief = _archive_brief(promotion=None)
    assert "no a4 decision" in brief.lower()


def test_archive_brief_includes_promotion_when_present():
    brief = _archive_brief(promotion={"decision": "approve", "rationale": "credible", "overfitting_risk": "low"})
    section = brief.split("## A4 promotion decision")[1]
    assert "approve" in section
    assert "credible" in section


def test_archive_prompt_version_is_stable_and_derived():
    a = archive_prompt_version()
    b = archive_prompt_version()
    assert a == b
    assert a.startswith("archive-v1-")


def _mine_brief(**overrides):
    fields = dict(family_failure_groups={}, existing_entries=[])
    fields.update(overrides)
    return assemble_mine_brief(**fields)


def test_mine_brief_omits_engine_internals():
    brief = _mine_brief(family_failure_groups={"fam": [{"experiment_id": 1, "failure_reason": "overfit_in_sample"}]})
    lowered = brief.lower()
    for term in _FORBIDDEN_TERMS:
        assert term not in lowered, f"forbidden term {term!r} leaked into the mine brief"


def test_mine_brief_includes_family_groups_and_existing_entries():
    brief = _mine_brief(
        family_failure_groups={"fam-a": [{"experiment_id": 1, "failure_reason": "overfit_in_sample"}]},
        existing_entries=[{"id": 5, "scope": "family", "title": "old lesson", "statement": "s"}],
    )
    groups_section = brief.split("## Recently-closed experiments")[1].split("## Existing")[0]
    assert "fam-a" in groups_section
    assert "overfit_in_sample" in groups_section
    entries_section = brief.split("## Existing family/market/global")[1]
    assert "old lesson" in entries_section
    assert '"id": 5' in entries_section


def test_mine_prompt_version_is_stable_and_derived():
    a = mine_prompt_version()
    b = mine_prompt_version()
    assert a == b
    assert a.startswith("mine-v1-")

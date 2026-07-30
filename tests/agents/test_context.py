"""The Implementation Brief (App-Flow §4.1) — what it contains, and what it
must never contain.
"""
from __future__ import annotations

from aqrl.agents.context import assemble_implement_brief, implement_prompt_version

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
    original = context_module._PROMPT_PATH.read_text(encoding="utf-8")
    fake = tmp_path / "implement_v1.md"
    fake.write_text(original + "\nan appended line that changes the hash\n", encoding="utf-8")
    monkeypatch.setattr(context_module, "_PROMPT_PATH", fake)
    assert implement_prompt_version() != before

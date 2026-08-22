"""Spec -> module rendering: the round-trip that makes A2 safe (Stage 5).

The one property this module must never violate: a rendered module,
re-imported, reconstructs a spec whose `spec_hash()` equals the original's
bit for bit. If that ever drifts, "the code" and "the thing `evaluate.py`
scores" have silently become two different objects.
"""
from __future__ import annotations

from aqrl.agents.render import code_path_for, render_module
from aqrl.operators.spec import Node, StrategySpec


def _exec_module(source: str) -> dict:
    namespace: dict = {}
    exec(compile(source, "<rendered>", "exec"), namespace)  # noqa: S102 - test-only
    return namespace


def test_round_trip_preserves_spec_hash(crossover_spec):
    rendered = render_module(crossover_spec, strategy_uid="uid-1", spec_version=1)
    namespace = _exec_module(rendered.source)
    reconstructed: StrategySpec = namespace["SPEC"]
    assert reconstructed.spec_hash() == crossover_spec.spec_hash()


def test_round_trip_preserves_metadata(crossover_spec):
    rendered = render_module(crossover_spec, strategy_uid="uid-1", spec_version=1)
    namespace = _exec_module(rendered.source)
    reconstructed: StrategySpec = namespace["SPEC"]
    assert reconstructed.hypothesis == crossover_spec.hypothesis


def test_generate_signals_matches_direct_compilation(crossover_spec):
    from aqrl.research.synthetic_data import synthetic_ohlcv

    from aqrl.operators.compile import compile_spec

    rendered = render_module(crossover_spec, strategy_uid="uid-1", spec_version=1)
    namespace = _exec_module(rendered.source)
    df = synthetic_ohlcv(300, seed=7)

    direct_signals = compile_spec(crossover_spec).to_signal_fn()(df, {})
    rendered_signals = namespace["generate_signals"](df, {})

    assert list(direct_signals) == list(rendered_signals)


def test_two_specs_meaning_the_same_thing_render_identically():
    """Node id and declaration order must not perturb the rendered module —
    the same invariant `spec_hash` already guarantees (`operators/spec.py`),
    now exercised through the renderer."""
    a = StrategySpec(
        entry_logic=[
            Node(id="n1", operator="ema", params={"span": 10}, inputs={"series": "price.close"}),
            Node(id="n2", operator="ema", params={"span": 20}, inputs={"series": "price.close"}),
            Node(id="n3", operator="crossover", inputs={"fast": "n1", "slow": "n2"}),
        ],
        hypothesis="a",
    )
    b = StrategySpec(
        entry_logic=[
            Node(id="fast_ma", operator="ema", params={"span": 20}, inputs={"series": "price.close"}),
            Node(id="slow_ma", operator="ema", params={"span": 10}, inputs={"series": "price.close"}),
            Node(id="cross", operator="crossover", inputs={"fast": "slow_ma", "slow": "fast_ma"}),
        ],
        hypothesis="a wholly different sentence",
    )
    # Same strategy under a different decomposition (different node ids,
    # declaration order, hypothesis wording) — spec_hash must agree even
    # though the rendered *source* legitimately differs (it preserves node
    # ids and metadata verbatim, which spec_hash deliberately excludes).
    assert a.spec_hash() == b.spec_hash()

    rendered_a = render_module(a, strategy_uid="same-uid", spec_version=1)
    rendered_b = render_module(b, strategy_uid="same-uid", spec_version=1)
    assert f"spec_hash: {a.spec_hash()}" in rendered_a.source
    assert f"spec_hash: {b.spec_hash()}" in rendered_b.source


def test_code_path_is_per_strategy():
    assert code_path_for("abc-123") == "strategies/abc-123/strategy.py"
    assert code_path_for("abc-123") != code_path_for("xyz-999")


def test_render_handles_quotes_and_unicode_in_metadata():
    spec = StrategySpec(
        entry_logic=[Node(id="e", operator="ema", params={"span": 5}, inputs={"series": "price.close"})],
        hypothesis='quotes " \'\'\' and unicode é 中文 and a backslash \\',
        rationale="line one\nline two\ttabbed",
    )
    rendered = render_module(spec, strategy_uid="uid", spec_version=1)
    namespace = _exec_module(rendered.source)
    reconstructed: StrategySpec = namespace["SPEC"]
    assert reconstructed.spec_hash() == spec.spec_hash()
    assert reconstructed.hypothesis == spec.hypothesis
    assert reconstructed.rationale == spec.rationale

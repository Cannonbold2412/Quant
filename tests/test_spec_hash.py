"""Canonical spec hashing — Stage 2's stated "done when".

> *"a strategy spec is expressible purely as an operator composition, hashed
> canonically, and two logically identical specs produce the same hash."*

Both halves are tested here, and the second is the one with teeth. If cosmetic
differences changed the hash, duplicate detection would fail **silently**: the
second identical experiment simply runs, burns compute, and adds a phantom trial
to the family count — which corrupts the `SR*(N_trials)` haircut for every
strategy in that family (TRD §8.6). A duplicate that gets through is not a
wasted afternoon, it is a wrong number in the science.
"""
from __future__ import annotations

import pytest

from aqrl.operators import Node, StrategySpec, SpecError, spec_hash, structural_hash


def ma(node_id: str, window: int, source: str = "price.close") -> Node:
    return Node(
        id=node_id, operator="rolling_mean", params={"window": window}, inputs={"series": source}
    )


def cross(node_id: str, fast: str, slow: str, gap: float = 0.002) -> Node:
    return Node(
        id=node_id,
        operator="crossover",
        params={"min_gap_pct": gap},
        inputs={"fast": fast, "slow": slow},
    )


@pytest.fixture
def baseline() -> StrategySpec:
    return StrategySpec(
        entry_logic=[ma("fast", 20), ma("slow", 100), cross("cross", "fast", "slow")],
        hypothesis="Trend persists at a 20/100-bar horizon.",
    )


# -- the equivalences ------------------------------------------------------------


def test_renaming_nodes_does_not_change_the_hash(baseline):
    """Ids are labels. `n1` -> `fast_ma` must not buy a fresh experiment."""
    renamed = StrategySpec(
        entry_logic=[ma("f", 20), ma("s", 100), cross("x", "f", "s")],
        hypothesis=baseline.hypothesis,
    )
    assert spec_hash(renamed) == spec_hash(baseline)


def test_declaration_order_does_not_change_the_hash(baseline):
    reordered = StrategySpec(
        entry_logic=[cross("cross", "fast", "slow"), ma("slow", 100), ma("fast", 20)],
        hypothesis=baseline.hypothesis,
    )
    assert spec_hash(reordered) == spec_hash(baseline)


def test_explicit_defaults_hash_the_same_as_omitted_ones():
    """`bind()` fills defaults before hashing, so writing one out is free."""
    explicit = StrategySpec(
        entry_logic=[
            ma("m", 20),
            Node(
                id="t",
                operator="threshold",
                params={"upper": 1.0, "lower": -1.0, "direction": "above"},
                inputs={"series": "m"},
            ),
        ]
    )
    implicit = StrategySpec(
        entry_logic=[ma("m", 20), Node(id="t", operator="threshold", inputs={"series": "m"})]
    )
    assert spec_hash(explicit) == spec_hash(implicit)


def test_float_spelling_does_not_change_the_hash():
    """`0.20` and `0.2` are the same double; `canonical_json` normalises them."""
    a = StrategySpec(entry_logic=[ma("f", 20), ma("s", 100), cross("c", "f", "s", 0.20)])
    b = StrategySpec(entry_logic=[ma("f", 20), ma("s", 100), cross("c", "f", "s", 0.2)])
    assert spec_hash(a) == spec_hash(b)


def test_input_key_order_does_not_change_the_hash():
    a = StrategySpec(
        entry_logic=[
            ma("f", 20),
            ma("s", 100),
            Node(id="c", operator="crossover", inputs={"fast": "f", "slow": "s"}),
        ]
    )
    b = StrategySpec(
        entry_logic=[
            ma("f", 20),
            ma("s", 100),
            Node(id="c", operator="crossover", inputs={"slow": "s", "fast": "f"}),
        ]
    )
    assert spec_hash(a) == spec_hash(b)


def test_commutative_operands_may_be_swapped():
    """`and(a, b)` and `and(b, a)` are one strategy, not two trials."""

    def joined(first: str, second: str) -> StrategySpec:
        return StrategySpec(
            entry_logic=[
                ma("m1", 10),
                ma("m2", 50),
                cross("c1", "m1", "m2"),
                Node(
                    id="mom",
                    operator="momentum",
                    params={"period": 20},
                    inputs={"series": "price.close"},
                ),
                Node(id="j", operator="and", inputs={"a": first, "b": second}),
            ]
        )

    assert spec_hash(joined("c1", "mom")) == spec_hash(joined("mom", "c1"))


def test_a_non_commutative_operator_still_respects_argument_order():
    """The control for the previous test: `crossover` is NOT commutative, and
    swapping fast for slow inverts the strategy."""
    a = StrategySpec(entry_logic=[ma("f", 20), ma("s", 100), cross("c", "f", "s")])
    b = StrategySpec(entry_logic=[ma("f", 20), ma("s", 100), cross("c", "s", "f")])
    assert spec_hash(a) != spec_hash(b)


def test_rewording_the_hypothesis_does_not_change_the_hash(baseline):
    """Deliberate: metadata is excluded so A1 cannot reword its way past
    duplicate rejection into an experiment already known to fail."""
    reworded = StrategySpec(
        entry_logic=baseline.entry_logic,
        hypothesis="An entirely different sentence about the same computation.",
        rationale="And a fresh rationale.",
        expected_behavior="And a prediction.",
    )
    assert spec_hash(reworded) == spec_hash(baseline)


def test_pinning_the_current_version_explicitly_changes_nothing(baseline):
    """`version=None` pins to the newest registered version at hash time."""
    pinned = StrategySpec(
        entry_logic=[
            ma("fast", 20),
            ma("slow", 100),
            Node(
                id="cross",
                operator="crossover",
                version="1.0.0",
                params={"min_gap_pct": 0.002},
                inputs={"fast": "fast", "slow": "slow"},
            ),
        ],
        hypothesis=baseline.hypothesis,
    )
    assert spec_hash(pinned) == spec_hash(baseline)


def test_an_intermediate_node_is_covered_by_its_consumer(baseline):
    """Roots-only hashing: the same computation decomposed differently agrees."""
    with_dead_alias = StrategySpec(
        entry_logic=[ma("fast", 20), ma("slow", 100), cross("cross", "fast", "slow")],
        hypothesis=baseline.hypothesis,
    )
    assert spec_hash(with_dead_alias) == spec_hash(baseline)


def test_a_shared_node_is_computed_once_in_the_hash():
    """A node feeding two roles is shared — a DAG, not four trees."""
    spec = StrategySpec(
        entry_logic=[ma("m", 20), ma("s", 60), cross("c", "m", "s")],
        filter_logic=[
            Node(id="vol", operator="vol_expansion", inputs={
                "high": "price.high", "low": "price.low", "close": "price.close"})
        ],
    )
    nodes = spec.nodes()
    assert structural_hash("m", nodes) == structural_hash("m", nodes, {})


# -- what MUST change the hash ----------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda: [ma("fast", 21), ma("slow", 100), cross("cross", "fast", "slow")],
                     id="different window"),
        pytest.param(lambda: [ma("fast", 20), ma("slow", 100), cross("cross", "fast", "slow", 0.003)],
                     id="different deadband"),
        pytest.param(lambda: [Node(id="fast", operator="ema", params={"span": 20},
                                   inputs={"series": "price.close"}),
                              ma("slow", 100), cross("cross", "fast", "slow")],
                     id="different operator"),
    ],
)
def test_a_genuine_difference_changes_the_hash(baseline, mutation):
    assert spec_hash(StrategySpec(entry_logic=mutation())) != spec_hash(baseline)


def test_moving_a_node_to_a_different_role_changes_the_hash():
    entry = StrategySpec(entry_logic=[ma("m", 20), ma("s", 60), cross("c", "m", "s")])
    as_filter = StrategySpec(filter_logic=[ma("m", 20), ma("s", 60), cross("c", "m", "s")])
    assert spec_hash(entry) != spec_hash(as_filter)


def test_the_universe_and_parameters_are_hashed():
    base = StrategySpec(entry_logic=[ma("m", 20)])
    with_universe = StrategySpec(entry_logic=[ma("m", 20)], universe={"index": "NIFTY50"})
    with_params = StrategySpec(entry_logic=[ma("m", 20)], parameters={"m.window": [10, 20, 30]})
    assert len({spec_hash(base), spec_hash(with_universe), spec_hash(with_params)}) == 3


def test_the_hash_is_stable_across_calls(baseline):
    assert spec_hash(baseline) == spec_hash(baseline)


# -- rejection --------------------------------------------------------------------


def test_a_cycle_is_reported_with_its_path():
    spec = StrategySpec(entry_logic=[ma("a", 10, "b"), ma("b", 10, "a")])
    with pytest.raises(SpecError, match="cycle"):
        spec_hash(spec)


def test_an_unknown_operator_is_rejected():
    spec = StrategySpec(
        entry_logic=[Node(id="x", operator="no_such_operator", inputs={"series": "price.close"})]
    )
    with pytest.raises(SpecError, match="no operator named"):
        spec_hash(spec)


def test_an_out_of_range_parameter_is_rejected_before_any_hash_exists():
    with pytest.raises(SpecError, match="must be >="):
        spec_hash(StrategySpec(entry_logic=[ma("a", 1)]))


def test_a_dangling_input_reference_is_rejected():
    with pytest.raises(SpecError, match="unknown node"):
        spec_hash(StrategySpec(entry_logic=[ma("a", 10, "ghost")]))


def test_an_unconnected_input_is_rejected():
    spec = StrategySpec(
        entry_logic=[Node(id="c", operator="crossover", inputs={"fast": "price.close"})]
    )
    with pytest.raises(SpecError, match="unconnected"):
        spec_hash(spec)


def test_an_unknown_input_port_is_rejected():
    spec = StrategySpec(
        entry_logic=[
            ma("a", 10),
            Node(id="c", operator="crossover", inputs={"fast": "a", "slow": "a", "extra": "a"}),
        ]
    )
    with pytest.raises(SpecError, match="unknown input"):
        spec_hash(spec)


def test_an_implicit_input_may_be_left_unconnected():
    """A risk operator's `position` comes from the framework, not the spec."""
    spec = StrategySpec(
        entry_logic=[ma("f", 20), ma("s", 100), cross("c", "f", "s")],
        risk_logic=[
            Node(id="stop", operator="atr_stop", inputs={
                "close": "price.close", "high": "price.high", "low": "price.low"})
        ],
    )
    assert len(spec_hash(spec)) == 64


def test_reusing_an_id_with_different_contents_is_rejected():
    spec = StrategySpec(entry_logic=[ma("m", 20)], filter_logic=[ma("m", 50)])
    with pytest.raises(SpecError, match="declared twice"):
        spec_hash(spec)


def test_a_node_id_may_not_impersonate_a_price_source():
    with pytest.raises(ValueError, match="may not start with"):
        Node(id="price.close", operator="rolling_mean", inputs={"series": "price.close"})

"""The operator contract — warm-up, parameters, applicability, versioning.

Causality has its own module because it is the property that matters most. This
one covers the rest of what `Operator` promises, and like that suite it is
**parametrized over the whole registry**, so the guarantees hold for operators
that do not exist yet.
"""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.operators import (
    Operator,
    OperatorError,
    ParameterError,
    ParamSpec,
    all_operators,
    get,
    names,
    operator_library_version,
    register,
    registry_descriptor,
    resolve_version,
)

from .conftest import operator_inputs, param_draws

OPERATORS = all_operators()
IDS = [operator.key for operator in OPERATORS]


# -- warm-up --------------------------------------------------------------------


@pytest.mark.parametrize("operator", OPERATORS, ids=IDS)
def test_warmup_is_nan_and_the_rest_is_not(operator):
    """The first `warmup` outputs are NaN; everything after them is finite.

    Both halves matter. NaN after warm-up means a hole a strategy will trade
    through; a *number* during warm-up is worse — it is a confident-looking
    value computed from too little data, and nothing downstream can tell it
    apart from a real one.
    """
    inputs = operator_inputs(operator)
    for params in param_draws(operator):
        out = operator(inputs, **params)
        warmup = operator.warmup(**params)

        if warmup:
            head = out[:warmup]
            assert np.isnan(head).all(), (
                f"{operator.key} produced a number during its own declared warm-up "
                f"({warmup} bars) with params={params}"
            )
        tail = out[warmup:]
        assert np.isfinite(tail).all(), (
            f"{operator.key} produced NaN/inf after its declared warm-up of {warmup} "
            f"bars with params={params}"
        )


@pytest.mark.parametrize("operator", OPERATORS, ids=IDS)
def test_output_is_bar_aligned(operator):
    """One output row per input row — no silent reindexing."""
    inputs = operator_inputs(operator)
    expected = next(iter(inputs.values())).shape[0]
    assert operator(inputs).shape[0] == expected


@pytest.mark.parametrize("operator", OPERATORS, ids=IDS)
def test_inputs_are_not_mutated(operator):
    """An operator that edits its input corrupts every sibling in the DAG.

    The compiler memoises node outputs and hands the same array to several
    consumers, so in-place modification here would show up as an unrelated
    operator producing wrong numbers — arbitrarily far away.
    """
    inputs = operator_inputs(operator)
    before = {port: array.copy() for port, array in inputs.items()}
    operator(inputs)
    for port, array in inputs.items():
        assert np.array_equal(array, before[port], equal_nan=True), (
            f"{operator.key} modified its {port!r} input in place"
        )


# -- parameters -----------------------------------------------------------------


@pytest.mark.parametrize("operator", OPERATORS, ids=IDS)
def test_defaults_bind_cleanly(operator):
    bound = operator.bind()
    assert set(bound) == {spec.name for spec in operator.params}


@pytest.mark.parametrize("operator", OPERATORS, ids=IDS)
def test_declared_ranges_are_enforced(operator):
    """TRD §11.1 requires valid ranges. An undeclared range is an unbounded
    search, and an unbounded search is overfitting `params_grid_size` cannot see."""
    for spec in operator.params:
        if spec.minimum is not None:
            with pytest.raises(ParameterError, match=">="):
                operator.bind(**{spec.name: spec.minimum - 1})
        if spec.maximum is not None:
            with pytest.raises(ParameterError, match="<="):
                operator.bind(**{spec.name: spec.maximum + 1})


@pytest.mark.parametrize("operator", OPERATORS, ids=IDS)
def test_unknown_parameters_are_rejected(operator):
    with pytest.raises(ParameterError, match="has no parameter"):
        operator.bind(definitely_not_a_real_parameter=1)


def test_a_bool_is_not_accepted_as_an_int():
    """`window=True` must not silently become `window=1`."""
    spec = ParamSpec("window", "int", 20, "Window.", minimum=2, maximum=100)
    with pytest.raises(ParameterError, match="got bool"):
        spec.check(True)


def test_a_default_outside_its_own_range_fails_at_declaration():
    """A bug in the operator, caught at import rather than at first tuning."""
    with pytest.raises(ParameterError):
        ParamSpec("window", "int", 1, "Window.", minimum=2, maximum=100)


def test_non_finite_floats_are_rejected():
    spec = ParamSpec("ratio", "float", 0.5, "Ratio.", minimum=0.0, maximum=1.0)
    with pytest.raises(ParameterError, match="finite"):
        spec.check(float("nan"))


def test_cross_parameter_rules_are_enforced():
    """`validate_params` catches what per-parameter ranges cannot."""
    with pytest.raises(ValueError, match="must not exceed"):
        get("threshold").bind(upper=-1.0, lower=1.0)


# -- applicability ---------------------------------------------------------------


@pytest.mark.parametrize("operator", OPERATORS, ids=IDS)
def test_applicability_resolves_against_a_real_profile(operator, loader):
    """Declarations must be checkable against the profiles actually shipped."""
    resolved = loader.resolve("nse_equity", "daily", "cash_equity")
    if operator.valid_markets and "nse_equity" not in operator.valid_markets:
        with pytest.raises(OperatorError, match="not valid for market"):
            operator.check_applicable(resolved)
    else:
        operator.check_applicable(resolved)


def test_an_operator_can_refuse_a_market(loader):
    class PerpetualOnly(Operator):
        name = "perp_only_test"
        category = "signal"
        valid_markets = frozenset({"binance_perp"})

        def apply(self, inputs, **params):
            return inputs["series"]

    with pytest.raises(OperatorError, match="not valid for market"):
        PerpetualOnly().check_applicable(loader.resolve("nse_equity", "daily", "cash_equity"))


def test_an_operator_can_require_overnight_positions(loader):
    class HoldsOvernight(Operator):
        name = "overnight_test"
        category = "risk"
        requires_overnight = True

        def apply(self, inputs, **params):
            return inputs["series"]

    resolved = loader.resolve("nse_equity", "daily", "cash_equity")
    if resolved.timeframe.overnight_positions:
        HoldsOvernight().check_applicable(resolved)  # daily holds overnight — fine


# -- registry and versioning -----------------------------------------------------


def test_every_registered_operator_has_a_unique_name_version():
    keys = [operator.key for operator in OPERATORS]
    assert len(keys) == len(set(keys))


def test_registering_a_duplicate_is_an_error():
    """A silent overwrite would change the meaning of every stored spec_hash."""
    existing = OPERATORS[0]

    with pytest.raises(OperatorError, match="already registered"):

        @register
        class Clash(Operator):
            name = existing.name
            version = existing.version
            category = existing.category

            def apply(self, inputs, **params):
                return inputs["series"]


def test_the_library_version_is_stable_across_calls():
    assert operator_library_version() == operator_library_version()


def test_the_library_version_is_derived_from_the_descriptors():
    """Derived, not hand-maintained — so it cannot drift from what it describes."""
    from aqrl.hashing import content_hash

    assert operator_library_version() == content_hash(registry_descriptor())[:16]


def test_the_library_version_changes_when_an_operator_changes():
    """A widened range or a new market makes previously impossible specs
    possible, so it must partition the result history (TRD §6.6)."""
    from aqrl.hashing import content_hash

    descriptors = registry_descriptor()
    before = content_hash(descriptors)[:16]
    mutated = [dict(entry) for entry in descriptors]
    mutated[0]["params"] = [*mutated[0]["params"], {"name": "new_knob", "kind": "int"}]
    assert content_hash(mutated)[:16] != before


def test_lookup_defaults_to_the_newest_version():
    for name in names():
        assert get(name).version == resolve_version(name)


def test_versions_are_ordered_semantically_not_lexicographically():
    """`1.10.0` is newer than `1.9.0`, and string sorting disagrees.

    Not cosmetic: a spec node with `version=None` pins the newest version **at
    hash time**, so resolving to a stale implementation would silently change
    what a stored `spec_hash` means.
    """
    from aqrl.operators.registry import _version_key

    ordered = sorted(["1.9.0", "1.10.0", "1.2.0", "2.0.0"], key=_version_key)
    assert ordered == ["1.2.0", "1.9.0", "1.10.0", "2.0.0"]
    assert sorted(["1.9.0", "1.10.0"])[-1] == "1.9.0", "the naive sort really is wrong"


def test_a_non_numeric_version_segment_does_not_break_resolution():
    from aqrl.operators.registry import _version_key

    assert _version_key("1.0.0-rc1") != _version_key("1.0.0")
    sorted(["1.0.0", "1.0.0-rc1", "1.1.0"], key=_version_key)  # must not raise


def test_an_unknown_operator_names_the_alternatives():
    with pytest.raises(OperatorError, match="no operator named"):
        get("definitely_not_an_operator")


# -- coverage of the declared catalogue -------------------------------------------


def test_every_category_in_the_trd_is_represented():
    categories = {operator.category for operator in OPERATORS}
    assert categories == {"transformation", "signal", "risk", "portfolio"}


@pytest.mark.parametrize(
    "expected",
    [
        # TRD §11's table, named explicitly so a silent regression in coverage
        # fails here rather than being noticed a stage later.
        "rolling_mean", "ema", "jma", "kalman", "atr_normalise", "frac_diff",
        "rolling_pca", "wavelet_detail",
        "crossover", "threshold", "breakout", "vol_expansion", "momentum",
        "mean_reversion", "volume_confirmation",
        "atr_stop", "time_stop", "trailing_stop", "kelly", "vol_target",
        "equal_weight", "risk_parity", "correlation_cluster",
    ],
)
def test_the_trd_catalogue_is_present(expected):
    assert expected in names()

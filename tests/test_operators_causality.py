"""Causality — truncation invariance over the entire operator registry.

**The single most important test in Stage 2.**

Look-ahead is the dominant failure mode of this project: the risk register
carries four separate Critical entries for it, and TRD §9.4 names vectorisation
as its top source. An operator library makes that risk worse, not better, unless
causality is guaranteed here — because `evaluate.py`'s P0 scan reads *strategy*
source and cannot see inside a library call. One operator that quietly peeks
silently biases every strategy ever composed from it.

The property, stated exactly:

> An operator's output at bar `t` must be identical whether or not it was ever
> shown bars after `t`.

Truncate the input at several checkpoints, recompute, and compare on the
overlap. Any mismatch means data flowed backwards in time. This is
`aqrl/eval/p0.py:empirical_leakage_scan` applied at operator
granularity rather than to a whole strategy.

The suite is **parametrized over `all_operators()`**, so a newly registered
operator is covered automatically and cannot enter the library untested —
TRD §11.1's *"an untested operator cannot enter the library"* enforced by
construction rather than by review discipline.

`test_the_scan_catches_a_deliberately_leaky_operator` is the control. Without
it, every assertion here could be passing vacuously and nobody would know.
"""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.operators import Operator, ParamSpec, all_operators

from .conftest import operator_inputs, param_draws

CHECKPOINTS = (0.5, 0.7, 0.85)
TOLERANCE = 1e-9

OPERATORS = all_operators()
IDS = [operator.key for operator in OPERATORS]


def _truncate(inputs: dict, k: int) -> dict:
    return {port: (array[:k] if array.ndim == 1 else array[:k, :]) for port, array in inputs.items()}


def _mismatch(full: np.ndarray, truncated: np.ndarray, k: int) -> float | None:
    """Largest disagreement on the overlap, or None if they agree."""
    a, b = full[:k], truncated[:k]
    # Bars where BOTH are NaN agree — both say "not enough data yet". A NaN on
    # one side only stays in the comparison and shows up as a difference, which
    # is right: warm-up that moves when history is truncated is itself a leak.
    comparable = ~(np.isnan(a) & np.isnan(b))
    if not comparable.any():
        return None
    difference = np.abs(np.nan_to_num(a[comparable]) - np.nan_to_num(b[comparable]))
    largest = float(difference.max())
    return None if largest <= TOLERANCE else largest


@pytest.mark.parametrize("operator", OPERATORS, ids=IDS)
def test_operator_is_causal(operator):
    """Output over `x[:k]` must equal output over the full `x` on the overlap."""
    full_inputs = operator_inputs(operator)

    for params in param_draws(operator):
        full = operator(full_inputs, **params)
        for fraction in CHECKPOINTS:
            k = int(full.shape[0] * fraction)
            truncated = operator(_truncate(full_inputs, k), **params)
            largest = _mismatch(full, truncated, k)
            assert largest is None, (
                f"{operator.key} leaked: truncating at {fraction:.0%} of history changed earlier "
                f"outputs by up to {largest:.3e} with params={params}. Future data flowed backwards."
            )


@pytest.mark.parametrize("operator", OPERATORS, ids=IDS)
def test_operator_is_deterministic(operator):
    """Identical inputs produce bit-identical outputs.

    TRD §20 requires a bit-identical `honest_score` on re-run, and §9.5 makes it
    a gate. That is unachievable if any operator is even slightly
    non-deterministic, so it is checked here where the cause would be obvious
    rather than at the end of a parallel walk-forward where it would not.
    """
    inputs = operator_inputs(operator)
    for params in param_draws(operator):
        first = operator(inputs, **params)
        second = operator(operator_inputs(operator), **params)
        assert np.array_equal(first, second, equal_nan=True), (
            f"{operator.key} produced different output for identical input with params={params}"
        )


def test_the_scan_catches_a_deliberately_leaky_operator():
    """The control: prove the scan can fail.

    Without this, every assertion above could be passing vacuously — a scan that
    cannot detect a leak is worse than no scan, because it manufactures
    confidence. This operator normalises by whole-series statistics, which is
    precisely the leak TRD §9.4 warns is the most common and the hardest to see.
    """

    class LeakyWholeSampleZScore(Operator):
        name = "leaky_test_only"
        category = "transformation"
        description = "Deliberately non-causal. Never registered."
        params = (ParamSpec("window", "int", 20, "Ignored.", minimum=2, maximum=100),)

        def apply(self, inputs, **params):
            series = inputs["series"]
            # Whole-series mean and std: every bar is normalised using the
            # future's statistics.
            return (series - series.mean()) / series.std()

    leaky = LeakyWholeSampleZScore()
    inputs = operator_inputs(leaky)
    full = leaky(inputs, window=20)

    caught = False
    for fraction in CHECKPOINTS:
        k = int(full.shape[0] * fraction)
        truncated = leaky(_truncate(inputs, k), window=20)
        if _mismatch(full, truncated, k) is not None:
            caught = True
            break
    assert caught, "the truncation scan failed to catch a whole-sample normalisation — it is broken"


def test_a_leaky_operator_cannot_hide_behind_a_shift():
    """A backwards shift is caught too, not just whole-sample statistics."""

    class PeeksAhead(Operator):
        name = "peeks_test_only"
        category = "signal"
        description = "Reads one bar into the future. Never registered."

        def apply(self, inputs, **params):
            series = inputs["series"]
            out = np.full(series.shape[0], np.nan, dtype=float)
            out[:-1] = series[1:]  # tomorrow's value, today
            return out

    peeks = PeeksAhead()
    inputs = operator_inputs(peeks)
    full = peeks(inputs)
    k = 200
    truncated = peeks(_truncate(inputs, k), **{})
    assert _mismatch(full, truncated, k) is not None, "a one-bar peek slipped past the scan"

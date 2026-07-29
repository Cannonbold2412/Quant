"""Spec → a runnable signal function.

This is what stops Stage 2 being scaffolding. `Implementation_Plan.md` §0 is
explicit that *"no stage exists purely as scaffolding"* — and a spec format that
cannot be executed is exactly that. With this module a hand-written spec runs
through the existing `nanoaqrl/evaluate.py` unchanged, with no Stage 3 and no
A2, which means the operator library is usable by hand the day it lands.

It also front-runs Stage 5. App-Flow §4.2 describes A2's job as *"translation,
not invention"* — assembling library blocks exactly as the spec describes. That
is a deterministic tree-walk, so it should be a Python function rather than a
prompt instruction an LLM might get creatively wrong.

**The output contract** matches `nanoaqrl/_lib/backtest.py`:

    def generate_signals(df: pd.DataFrame, params: dict) -> pd.Series   # in [-1, 1]

and the central lag (`position[t] = signal[t-1]`) is still applied by the
backtest, not here. Warm-up NaNs are converted to 0.0 — flat — at the very last
step, because "no position" is the honest reading of "not enough data yet", and
NaN would silently poison the return series.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from ..profiles.models import ResolvedProfile
from .base import Operator
from .spec import SOURCE_PREFIX, Node, Role, SpecError, StrategySpec, structural_hash

__all__ = ["CompiledSpec", "SignalFn", "compile_spec", "spec_warmup"]

SignalFn = Callable[[pd.DataFrame, dict], pd.Series]


class CompiledSpec:
    """A spec resolved against a profile and ready to evaluate.

    Holds the DAG, not the data — so one compiled spec is reused across every
    walk-forward fold, which matters when a fold-parallel run compiles once and
    evaluates dozens of times.
    """

    def __init__(self, spec: StrategySpec, resolved: ResolvedProfile | None = None) -> None:
        spec.validate_spec()
        self.spec = spec
        self.resolved = resolved
        self.nodes = spec.nodes()
        self.spec_hash = spec.spec_hash()

        if resolved is not None:
            for _, operator, _ in _all_resolved(spec):
                operator.check_applicable(resolved)

    # -- evaluation ------------------------------------------------------------

    def signals(self, frame: pd.DataFrame, params: dict[str, Any] | None = None) -> pd.Series:
        """Evaluate the DAG over `frame` and return the directional signal."""
        columns = {name: frame[name].to_numpy(dtype=float) for name in frame.columns}
        return pd.Series(self.signals_array(columns, params), index=frame.index)

    def signals_array(
        self, columns: dict[str, np.ndarray], params: dict[str, Any] | None = None
    ) -> np.ndarray:
        """The same evaluation, over plain arrays.

        `signals` is the pandas-shaped contract nanoAQRL's backtest expects;
        this is the shape Stage 3's engine wants, since it holds a panel of
        NumPy columns and would otherwise build and discard a DataFrame per
        instrument per fold. Both paths run identical code — the DAG is
        evaluated here and `signals` only re-wraps the result.
        """
        overrides = params or {}
        columns = {name: np.asarray(values, dtype=float) for name, values in columns.items()}
        n = len(next(iter(columns.values()))) if columns else 0
        cache: dict[str, np.ndarray] = {}

        entry = self._combine("entry", columns, overrides, cache, n)
        exit_signal = self._combine("exit", columns, overrides, cache, n)
        filters = self._combine("filter", columns, overrides, cache, n)

        position = entry
        if _present(self.spec, "exit"):
            # An exit fires where it is non-zero: flatten there rather than
            # reversing, because "get out" and "go the other way" are different
            # claims and a spec that meant the latter says so with an entry.
            position = np.where(exit_signal != 0.0, 0.0, position)
        if _present(self.spec, "filter"):
            # A filter gates; it never introduces direction of its own.
            position = np.where(filters > 0.0, position, 0.0)

        position = self._apply_risk(position, columns, overrides, cache, n)

        # NaN means "warm-up not finished". Flat is the honest position there.
        return np.clip(np.nan_to_num(position, nan=0.0), -1.0, 1.0)

    def to_signal_fn(self) -> SignalFn:
        """The `(df, params) -> Series` callable nanoAQRL's backtest expects."""

        def generate_signals(df: pd.DataFrame, params: dict) -> pd.Series:
            return self.signals(df, params)

        generate_signals.__doc__ = (
            f"Compiled from spec_hash={self.spec_hash}. "
            "Signals are computed from data at or before each bar; the entry lag is applied "
            "centrally by evaluate.py."
        )
        return generate_signals

    # -- internals -------------------------------------------------------------

    def _combine(
        self,
        role: Role,
        columns: dict[str, np.ndarray],
        overrides: dict[str, Any],
        cache: dict[str, np.ndarray],
        n: int,
    ) -> np.ndarray:
        """Evaluate a role's roots and reduce them to one series.

        Multiple roots in one role are ANDed for entry and filter (every
        condition must hold) and ORed for exit (any exit fires). Requiring
        agreement is the conservative default: a spec that wants "either signal"
        composes an explicit `or` node and says so.
        """
        roots = self.spec.roots(role)
        if not roots:
            return np.zeros(n, dtype=float)

        series = [self._evaluate(node.id, columns, overrides, cache) for node in roots]
        if len(series) == 1:
            return series[0]

        stacked = np.vstack(series)
        if role == "exit":
            fired = np.nansum(np.abs(np.nan_to_num(stacked)), axis=0)
            return (fired > 0).astype(float)

        signs = np.sign(np.nan_to_num(stacked))
        long_all = np.all(signs > 0, axis=0).astype(float)
        short_all = np.all(signs < 0, axis=0).astype(float)
        agreed = long_all - short_all
        # A role of pure boolean filters has no sign to agree on; fall back to
        # "every one of them is true".
        if role == "filter":
            positive = np.all(np.nan_to_num(stacked) > 0, axis=0).astype(float)
            return np.where(agreed != 0.0, agreed, positive)
        return agreed

    def _apply_risk(
        self,
        position: np.ndarray,
        columns: dict[str, np.ndarray],
        overrides: dict[str, Any],
        cache: dict[str, np.ndarray],
        n: int,
    ) -> np.ndarray:
        """Chain risk operators over the position, in declaration order.

        Order is significant here and nowhere else in a spec — a time stop after
        a trailing stop is not the same strategy as the reverse — so `risk_logic`
        keeps its declared sequence rather than being sorted.
        """
        for node in self.spec.roots("risk"):
            operator, bound = _resolve_node(self.nodes[node.id], overrides)
            inputs: dict[str, np.ndarray] = {}
            for port in operator.inputs:
                if port in operator.implicit_inputs and port not in node.inputs:
                    inputs[port] = position
                else:
                    inputs[port] = self._input(node, port, columns, overrides, cache, n)
            position = operator(inputs, **bound)
        return position

    def _input(
        self,
        node: Node,
        port: str,
        columns: dict[str, np.ndarray],
        overrides: dict[str, Any],
        cache: dict[str, np.ndarray],
        n: int,
    ) -> np.ndarray:
        reference = node.inputs.get(port)
        if reference is None:
            raise SpecError(f"node {node.id!r} leaves input {port!r} unconnected")
        if reference.startswith(SOURCE_PREFIX):
            return _source(reference, columns, n)
        return self._evaluate(reference, columns, overrides, cache)

    def _evaluate(
        self,
        node_id: str,
        columns: dict[str, np.ndarray],
        overrides: dict[str, Any],
        cache: dict[str, np.ndarray],
    ) -> np.ndarray:
        """Evaluate one node, memoised on its **structural** hash.

        Structural rather than by id, so two differently-named nodes computing
        the same thing are computed once. In a spec where a filter and an entry
        both derive from the same 50-bar mean, that halves the work for free.
        """
        node = self.nodes[node_id]
        key = structural_hash(node_id, self.nodes)
        if key in cache:
            return cache[key]

        operator, bound = _resolve_node(node, overrides)
        n = len(next(iter(columns.values()))) if columns else 0
        inputs = {
            port: self._input(node, port, columns, overrides, cache, n)
            for port in operator.inputs
        }
        result = operator(inputs, **bound)
        cache[key] = result
        return result


# -- helpers --------------------------------------------------------------------


def _present(spec: StrategySpec, role: Role) -> bool:
    return bool(spec.roots(role))


def _source(reference: str, columns: dict[str, np.ndarray], n: int) -> np.ndarray:
    """Resolve `price.<column>` against the frame, with one derived exception."""
    name = reference[len(SOURCE_PREFIX) :]
    if name in columns:
        return columns[name]
    # `price.returns` is not a stored column but every risk operator wants it,
    # and deriving it here keeps every spec from re-declaring the same node.
    if name == "returns" and "close" in columns:
        close = columns["close"]
        returns = np.full(n, np.nan, dtype=float)
        if n > 1:
            with np.errstate(divide="ignore", invalid="ignore"):
                returns[1:] = np.where(close[:-1] != 0, close[1:] / close[:-1] - 1.0, np.nan)
        return returns
    raise SpecError(
        f"{reference!r} is not available (frame columns: {', '.join(sorted(columns)) or 'none'})"
    )


def _resolve_node(node: Node, overrides: dict[str, Any]) -> tuple[Operator, dict[str, Any]]:
    """Resolve a node, letting spec-level `parameters` override node params.

    This is how `evaluate.py` tunes a spec per fold without rewriting it: the
    tuner passes `{"<node_id>.<param>": value}` and the DAG is unchanged.
    """
    from .spec import _resolve  # local import: spec.py owns the error wrapping

    operator, bound = _resolve(node)
    scoped = {
        key.split(".", 1)[1]: value
        for key, value in overrides.items()
        if key.startswith(f"{node.id}.")
    }
    if scoped:
        bound = operator.bind(**{**node.params, **scoped})
    return operator, bound


def _all_resolved(spec: StrategySpec):
    for entries in spec.operators().values():
        yield from entries


def spec_warmup(spec: StrategySpec) -> int:
    """Bars consumed before the whole spec produces meaningful output.

    The sum along the longest dependency chain, not the maximum over nodes: a
    50-bar mean feeding a 20-bar z-score needs 69 bars, not 49. Stage 3 needs
    this to size purge and embargo windows correctly, and understating it is how
    a fold silently trains on its own warm-up.
    """
    nodes = spec.nodes()
    memo: dict[str, int] = {}

    def depth(node_id: str) -> int:
        if node_id in memo:
            return memo[node_id]
        node = nodes[node_id]
        operator, bound = _resolve_node(node, {})
        upstream = [
            depth(reference)
            for reference in node.inputs.values()
            if not reference.startswith(SOURCE_PREFIX)
        ]
        memo[node_id] = operator.warmup(**bound) + (max(upstream) if upstream else 0)
        return memo[node_id]

    signal_warmup = max(
        (
            depth(node.id)
            for role in ("entry", "exit", "filter")
            for node in spec.roots(role)
        ),
        default=0,
    )
    # A risk operator's position arrives implicitly from the signal chain, so it
    # is not in `node.inputs` and `depth` cannot see it. Its warm-up therefore
    # stacks on top of the signal's rather than standing alone — an ATR stop on
    # a 100-bar crossover needs 114 bars, not 14.
    risk_warmup = max((depth(node.id) for node in spec.roots("risk")), default=0)
    return max(signal_warmup, signal_warmup + risk_warmup if spec.roots("risk") else 0)


def compile_spec(spec: StrategySpec, resolved: ResolvedProfile | None = None) -> CompiledSpec:
    """Validate a spec against the registry and return an evaluable form."""
    return CompiledSpec(spec, resolved)

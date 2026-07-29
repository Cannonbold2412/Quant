"""The operator library — the vetted vocabulary strategies compose from (TRD §11).

**The LLM does not invent arbitrary formulas.** It composes from this library.
That shrinks the search space enormously, keeps strategies interpretable, and
makes results comparable across experiments — which is what lets A5 draw a
lesson from experiment #6,201 in crypto and apply it to #12,483 in Indian
equities.

Importing this package registers every operator. The category modules are
imported for their side effects, so `aqrl.operators.registry.all_operators()`
is complete after a single `import aqrl.operators`.

    from aqrl.operators import StrategySpec, Node, compile_spec, spec_hash

    spec = StrategySpec(
        entry_logic=[
            Node(id="fast", operator="rolling_mean", params={"window": 20},
                 inputs={"series": "price.close"}),
            Node(id="slow", operator="rolling_mean", params={"window": 100},
                 inputs={"series": "price.close"}),
            Node(id="cross", operator="crossover", params={"min_gap_pct": 0.002},
                 inputs={"fast": "fast", "slow": "slow"}),
        ],
        hypothesis="Trend persists at a 20/100-bar horizon.",
    )
    signals = compile_spec(spec).to_signal_fn()   # runs in nanoaqrl/evaluate.py
"""
from __future__ import annotations

# Registration side effects. Order is irrelevant — the registry sorts.
from . import portfolio, risk, signals, transforms  # noqa: F401
from .base import Category, Operator, OperatorError, ParameterError, ParamSpec
from .compile import CompiledSpec, SignalFn, compile_spec, spec_warmup
from .registry import (
    all_operators,
    get,
    names,
    operator_library_version,
    register,
    registry_descriptor,
    resolve_version,
)
from .spec import Node, Role, SpecError, StrategySpec, node_operators, spec_hash, structural_hash

__all__ = [
    "Category",
    "CompiledSpec",
    "Node",
    "Operator",
    "OperatorError",
    "ParamSpec",
    "ParameterError",
    "Role",
    "SignalFn",
    "SpecError",
    "StrategySpec",
    "all_operators",
    "compile_spec",
    "get",
    "names",
    "node_operators",
    "operator_library_version",
    "register",
    "registry_descriptor",
    "resolve_version",
    "spec_hash",
    "spec_warmup",
    "structural_hash",
]

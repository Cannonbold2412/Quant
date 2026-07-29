"""The operator contract — what every building block must guarantee.

**The LLM does not invent arbitrary formulas** (PRD §6.1, TRD §11). It composes
from a vetted, versioned library. That shrinks the search space enormously,
makes strategies interpretable, and — the part that matters for the science —
makes results comparable across experiments.

Three contracts, all machine-checked by the registry-wide suites in
`tests/test_operators_causality.py` and `tests/test_operators_contract.py`:

1. **Causal.** `out[t]` depends only on `inputs[:t+1]`. No whole-series
   statistics, no centred windows, no backfill.
2. **Warm-up is NaN, and only warm-up is NaN.** The first `warmup(**params)`
   outputs are NaN; after that they are finite.
3. **Pure and deterministic.** No I/O, no global state, no clock, and no RNG
   unless a `seed` parameter is declared.

### Why causality is enforced *here*

Look-ahead is the dominant failure mode of this entire project — the risk
register carries four separate 🔴 Critical entries for it, and TRD §9.4 names
vectorisation as its top source. `evaluate.py`'s P0 scan reads *strategy*
source; it cannot see inside a library call. So an operator that quietly peeks
is a leak amplifier: every strategy composed from it inherits the bias, and
nothing downstream will ever report it.

Enforcing causality at this boundary — by property test, over the whole
registry, so a new operator is covered the moment it is registered — is the
only place the guarantee can be made once instead of per strategy.

### Why NumPy and not pandas or polars

TRD §19 specifies NumPy + Polars. Plain `np.ndarray` in and out keeps operators
neutral between the polars data layer and nanoAQRL's pandas backtest, keeps
index-alignment surprises out of the maths, and is the only form Numba can JIT
when TRD §9.2's path-dependent loops are eventually profiled.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

import numpy as np

from ..profiles.models import ResolvedProfile

__all__ = [
    "Operator",
    "OperatorError",
    "ParamSpec",
    "ParameterError",
    "Category",
]

Category = Literal["transformation", "signal", "risk", "portfolio"]

ParamKind = Literal["int", "float", "bool", "str"]

_KIND_TYPES: dict[str, tuple[type, ...]] = {
    "int": (int,),
    "float": (int, float),  # an int is an acceptable float; the reverse is not
    "bool": (bool,),
    "str": (str,),
}


class OperatorError(ValueError):
    """An operator was declared, parameterised or applied incorrectly."""


class ParameterError(OperatorError):
    """A parameter is unknown, mistyped, or outside its declared valid range."""


class ParamSpec:
    """One parameter, with the valid range TRD §11.1 requires it to declare.

    The range is not decoration. A1 proposes parameter *ranges* and `evaluate.py`
    tunes within them per fold; an undeclared range means an unbounded search,
    and an unbounded search is how a grid quietly becomes a source of overfitting
    that `params_grid_size` cannot account for.
    """

    __slots__ = ("name", "kind", "default", "minimum", "maximum", "choices", "description")

    def __init__(
        self,
        name: str,
        kind: ParamKind,
        default: Any,
        description: str,
        minimum: float | None = None,
        maximum: float | None = None,
        choices: tuple[str, ...] | None = None,
    ) -> None:
        if kind not in _KIND_TYPES:
            raise OperatorError(f"parameter {name!r} has unknown kind {kind!r}")
        self.name = name
        self.kind = kind
        self.default = default
        self.description = description
        self.minimum = minimum
        self.maximum = maximum
        self.choices = choices
        # A default outside its own declared range is a bug in the operator, not
        # in the caller — fail at import rather than at the first tuning run.
        self.check(default)

    def check(self, value: Any) -> Any:
        """Type- and range-check one value, returning it normalised."""
        expected = _KIND_TYPES[self.kind]
        # bool is a subclass of int; accepting it as one silently turns
        # `window=True` into `window=1`.
        if isinstance(value, bool) and self.kind != "bool":
            raise ParameterError(f"{self.name} must be {self.kind}, got bool {value!r}")
        if not isinstance(value, expected):
            raise ParameterError(
                f"{self.name} must be {self.kind}, got {type(value).__name__} {value!r}"
            )

        if self.kind == "float":
            value = float(value)
            if not math.isfinite(value):
                raise ParameterError(f"{self.name} must be finite, got {value!r}")
        if self.choices is not None and value not in self.choices:
            raise ParameterError(
                f"{self.name} must be one of {list(self.choices)}, got {value!r}"
            )
        if self.minimum is not None and value < self.minimum:
            raise ParameterError(f"{self.name} must be >= {self.minimum}, got {value!r}")
        if self.maximum is not None and value > self.maximum:
            raise ParameterError(f"{self.name} must be <= {self.maximum}, got {value!r}")
        return value

    def descriptor(self) -> dict[str, Any]:
        """The hashable, storable form (`operators.parameters`)."""
        return {
            "name": self.name,
            "kind": self.kind,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices) if self.choices else None,
            "description": self.description,
        }


class Operator(ABC):
    """A vetted, versioned, unit-tested building block.

    Subclasses declare metadata as class attributes and implement `apply`.
    Instances are stateless and shared — never store per-call data on `self`,
    or fold-level parallelism (TRD §9.3, processes not threads) stops being
    bit-identical.
    """

    name: ClassVar[str]
    version: ClassVar[str] = "1.0.0"
    category: ClassVar[Category]
    description: ClassVar[str] = ""
    references: ClassVar[str | None] = None

    #: Named input ports, e.g. `("series",)` or `("high", "low", "close")`.
    inputs: ClassVar[tuple[str, ...]] = ("series",)
    #: Ports the *framework* supplies rather than the spec. A risk operator's
    #: `position` is the running position from entry/exit/filter, so requiring
    #: every spec to name an internal node for it would be noise — and noise a
    #: spec author could get wrong. A spec may still wire one explicitly.
    implicit_inputs: ClassVar[frozenset[str]] = frozenset()
    params: ClassVar[tuple[ParamSpec, ...]] = ()

    #: `None` means "every market" / "every timeframe" — the common case. A set
    #: narrows it, e.g. funding operators to perpetuals only.
    valid_markets: ClassVar[frozenset[str] | None] = None
    valid_timeframes: ClassVar[frozenset[str] | None] = None
    #: Requires positions to survive a session boundary.
    requires_overnight: ClassVar[bool] = False

    #: When true, argument ORDER carries no meaning — `and(a, b) == and(b, a)`.
    #: The spec hasher sorts the children of a commutative node so two logically
    #: identical specs cannot differ by argument order (see `spec.py`).
    commutative: ClassVar[bool] = False

    # -- parameters ------------------------------------------------------------

    def bind(self, **params: Any) -> dict[str, Any]:
        """Fill defaults, reject unknown names, type- and range-check the rest.

        Always called before hashing a spec node, so a parameter written at its
        default and one left implicit produce the same `spec_hash`.
        """
        specs = {spec.name: spec for spec in self.params}
        unknown = set(params) - set(specs)
        if unknown:
            raise ParameterError(
                f"{self.name} has no parameter(s) {', '.join(sorted(unknown))} "
                f"(declared: {', '.join(specs) or 'none'})"
            )
        bound = {name: spec.check(params.get(name, spec.default)) for name, spec in specs.items()}
        return self.validate_params(bound)

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Hook for cross-parameter rules, e.g. `fast_window < slow_window`."""
        return params

    # -- applicability ---------------------------------------------------------

    def check_applicable(self, resolved: ResolvedProfile) -> None:
        """Refuse a market/timeframe this operator does not claim to support."""
        market, timeframe = resolved.market.name, resolved.timeframe.name
        if self.valid_markets is not None and market not in self.valid_markets:
            raise OperatorError(
                f"{self.name} is not valid for market {market!r} "
                f"(declared: {', '.join(sorted(self.valid_markets))})"
            )
        if self.valid_timeframes is not None and timeframe not in self.valid_timeframes:
            raise OperatorError(
                f"{self.name} is not valid for timeframe {timeframe!r} "
                f"(declared: {', '.join(sorted(self.valid_timeframes))})"
            )
        if self.requires_overnight and not resolved.timeframe.overnight_positions:
            raise OperatorError(
                f"{self.name} needs positions to survive a session boundary, but "
                f"{timeframe!r} declares overnight_positions = false"
            )

    # -- computation -----------------------------------------------------------

    def warmup(self, **params: Any) -> int:
        """Bars consumed before the output is defined. Default: none."""
        return 0

    @abstractmethod
    def apply(self, inputs: dict[str, np.ndarray], **params: Any) -> np.ndarray:
        """Compute the output. `params` arrive already bound and checked.

        **Must be causal**: `out[t]` may read `inputs[..., :t+1]` and nothing
        later. The registry-wide truncation-invariance test enforces this.
        """

    def __call__(self, inputs: dict[str, np.ndarray], **params: Any) -> np.ndarray:
        """Bind, check inputs, apply. The entry point compilation uses."""
        bound = self.bind(**params)
        missing = [port for port in self.inputs if port not in inputs]
        if missing:
            raise OperatorError(
                f"{self.name} needs input(s) {', '.join(missing)} "
                f"(got: {', '.join(inputs) or 'none'})"
            )

        arrays = {port: np.asarray(inputs[port], dtype=float) for port in self.inputs}
        lengths = {array.shape[0] for array in arrays.values()}
        if len(lengths) > 1:
            raise OperatorError(
                f"{self.name} received inputs of differing lengths: "
                + ", ".join(f"{port}={arrays[port].shape[0]}" for port in self.inputs)
            )

        result = np.asarray(self.apply(arrays, **bound), dtype=float)
        if result.shape[0] != next(iter(lengths), 0):
            raise OperatorError(
                f"{self.name} returned {result.shape[0]} rows for "
                f"{next(iter(lengths), 0)} input rows; operators are bar-aligned"
            )
        return result

    # -- identity --------------------------------------------------------------

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def descriptor(self) -> dict[str, Any]:
        """The canonical description hashed into `operator_library_version`.

        Everything here changes the meaning of a result, so everything here must
        move the version: a widened parameter range or a newly-claimed market
        makes previously-impossible specs possible.
        """
        return {
            "name": self.name,
            "version": self.version,
            "category": self.category,
            "inputs": list(self.inputs),
            "implicit_inputs": sorted(self.implicit_inputs),
            "params": [spec.descriptor() for spec in self.params],
            "valid_markets": sorted(self.valid_markets) if self.valid_markets else None,
            "valid_timeframes": sorted(self.valid_timeframes) if self.valid_timeframes else None,
            "requires_overnight": self.requires_overnight,
            "commutative": self.commutative,
            "implementation_ref": f"{type(self).__module__}.{type(self).__qualname__}",
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.key}>"

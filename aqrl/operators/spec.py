"""The strategy spec — an operator DAG, and its canonical hash.

A spec is *"a composition of operators expressible as a DAG"* (TRD §11.1),
which makes strategies diffable, searchable and — the part this module exists
for — **hashable**. `spec_hash` is checked against every prior spec before any
compute is spent (App-Flow §3.4): an exact match is rejected outright.

### Why structural hashing, and not just hashing the JSON

Hashing the spec document directly would make `spec_hash` a hash of *how the
spec was written* rather than *what it computes*. Duplicate detection would then
fail on any cosmetic difference, and it would fail silently — the second
identical experiment simply runs, burns compute, and inflates the family trial
count with a phantom trial. That corrupts the deflated Sharpe of every strategy
in the family, because the haircut `SR*(N_trials)` is only honest if `N_trials`
is exact (TRD §8.6).

So each node is reduced to a **structural hash** computed bottom-up:

    struct(node) = content_hash({
        "op":     "name@version",              # version pinned at hash time
        "params": bound_params,                # defaults filled in
        "inputs": {port: struct(child) | ref},  # children sorted if commutative
    })

Six things therefore cannot change the hash, all of which leave the computation
identical:

| Differing only in | Why the hash is unchanged |
|---|---|
| node ids (`n1` vs `fast_ma`) | ids never enter — only structure does |
| declaration order within a role | roles hash as a sorted list of structural hashes |
| a parameter written at its default vs omitted | `bind()` fills defaults first |
| dict key order, float spelling (`0.20` / `0.2`) | `canonical_json` normalises both |
| argument order into a commutative operator | children sorted by structural hash |
| hypothesis / rationale wording | metadata is excluded — see below |

**Metadata is deliberately excluded.** If `hypothesis` were hashed, A1 could
reword a sentence and buy itself a fresh run of an experiment already known to
fail. Duplicate detection has to bind to the computation, or it does not bind at
all.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..hashing import content_hash
from .base import Operator, OperatorError
from .registry import get, resolve_version

__all__ = [
    "Node",
    "Role",
    "SpecError",
    "StrategySpec",
    "node_operators",
    "spec_hash",
    "structural_hash",
]

Role = Literal["entry", "exit", "filter", "risk"]
ROLES: tuple[Role, ...] = ("entry", "exit", "filter", "risk")

#: A leaf reference into the market data rather than another node. `price.close`,
#: `price.volume`, and so on — the ports `compile.py` knows how to satisfy.
SOURCE_PREFIX = "price."

#: Bumped only if the canonical form itself changes shape. Every stored hash
#: depends on it, so a bump partitions the entire spec history — which is the
#: honest outcome when the meaning of a hash changes, and the reason it is
#: recorded explicitly rather than left implicit.
CANONICAL_SCHEMA_VERSION = 1


class SpecError(ValueError):
    """A spec is malformed: unknown operator, dangling input, or a cycle."""


class Node(BaseModel):
    """One operator application within the DAG."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    #: `None` pins to the registry's newest version **at hash time**, so a stored
    #: hash always names a concrete implementation even when the author did not.
    version: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    #: port -> another node's id, or `price.<column>`.
    inputs: dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _no_source_collision(cls, value: str) -> str:
        if value.startswith(SOURCE_PREFIX):
            raise ValueError(f"node id {value!r} may not start with {SOURCE_PREFIX!r}")
        return value


class StrategySpec(BaseModel):
    """A full strategy, shaped to map 1:1 onto `strategy_specs` columns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_logic: list[Node] = Field(default_factory=list)
    exit_logic: list[Node] = Field(default_factory=list)
    filter_logic: list[Node] = Field(default_factory=list)
    risk_logic: list[Node] = Field(default_factory=list)
    universe: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)

    # Metadata — carried through to the database, never hashed.
    hypothesis: str = ""
    rationale: str | None = None
    expected_behavior: str | None = None

    def role_nodes(self, role: Role) -> list[Node]:
        return list(getattr(self, f"{role}_logic"))

    def roots(self, role: Role) -> list[Node]:
        """The nodes a role actually *outputs*, derived rather than declared.

        A role lists every node it needs — the two moving averages *and* the
        crossover that consumes them. Only the crossover is the answer; the
        means are intermediate values. Treating every declared node as an output
        would combine a price series with a signal, which is meaningless.

        So a root is a node **no other node in the same role consumes**. That
        matches how a DAG is naturally written (list the pieces; the one nothing
        depends on is the result) and needs no extra syntax to declare.

        It also improves the hash: an intermediate node is already covered by its
        consumer's structural hash, so two specs that decompose the same
        computation differently still agree.
        """
        declared = self.role_nodes(role)
        consumed = {
            reference for node in declared for reference in node.inputs.values()
        }
        return [node for node in declared if node.id not in consumed]

    def nodes(self) -> dict[str, Node]:
        """Every node across every role, keyed by id.

        Ids are global to the spec on purpose: a node consumed by two roles is
        *shared*, which is what makes this a DAG rather than four trees, and
        what lets a filter feed both entry and exit without being computed twice.
        """
        collected: dict[str, Node] = {}
        for role in ROLES:
            for node in self.role_nodes(role):
                existing = collected.get(node.id)
                if existing is not None and existing != node:
                    raise SpecError(
                        f"node id {node.id!r} is declared twice with different contents; "
                        "ids are shared across roles, so reuse must be identical"
                    )
                collected[node.id] = node
        return collected

    # -- hashing ---------------------------------------------------------------

    def canonical(self) -> dict[str, Any]:
        """The structure `spec_hash` is taken over."""
        nodes = self.nodes()
        _check_acyclic(nodes)
        cache: dict[str, str] = {}
        return {
            "schema": CANONICAL_SCHEMA_VERSION,
            # Roots only: an intermediate node is already folded into its
            # consumer's structural hash, so hashing the whole declaration list
            # would let a cosmetic re-decomposition look like a new experiment.
            **{
                role: sorted(structural_hash(node.id, nodes, cache) for node in self.roots(role))
                for role in ROLES
            },
            "universe": self.universe,
            "parameters": self.parameters,
        }

    def spec_hash(self) -> str:
        return content_hash(self.canonical())

    # -- validation ------------------------------------------------------------

    def validate_spec(self) -> None:
        """Resolve every operator, bind every parameter, check every reference.

        Called before the hash is trusted and before compilation. Failing here
        costs nothing; failing later costs an experiment slot and a row in the
        results table that means nothing.
        """
        self.canonical()

    def operators(self) -> dict[Role, list[tuple[str, Operator, dict[str, Any]]]]:
        """Per role: `(node_id, operator, bound_params)` for every reachable node.

        This is what populates `spec_operators`, so *"which experiments ever used
        a Kalman filter?"* is an indexed join rather than a scan over JSON
        (Backend-Schema §15 Q4).
        """
        nodes = self.nodes()
        return {
            role: [
                (node_id, *_resolve(nodes[node_id]))
                for node_id in sorted(node_operators(self, role))
            ]
            for role in ROLES
        }


# -- structural hashing ---------------------------------------------------------


def _resolve(node: Node) -> tuple[Operator, dict[str, Any]]:
    """Look the operator up and bind its parameters, or explain why not."""
    try:
        version = node.version or resolve_version(node.operator)
        operator = get(node.operator, version)
        return operator, operator.bind(**node.params)
    except OperatorError as exc:
        raise SpecError(f"node {node.id!r}: {exc}") from exc


def structural_hash(node_id: str, nodes: dict[str, Node], cache: dict[str, str] | None = None) -> str:
    """The hash of what a node *computes*, ignoring what it is called.

    Memoised, so a shared subgraph is hashed once no matter how many roles
    consume it.
    """
    cache = {} if cache is None else cache
    if node_id in cache:
        return cache[node_id]

    node = nodes.get(node_id)
    if node is None:
        raise SpecError(f"input references unknown node {node_id!r}")

    operator, bound = _resolve(node)

    # Implicit ports are supplied by the compiler (a risk operator's `position`
    # is the running position from entry/exit/filter), so leaving them
    # unconnected is the normal case rather than an error.
    missing = [
        port
        for port in operator.inputs
        if port not in node.inputs and port not in operator.implicit_inputs
    ]
    if missing:
        raise SpecError(
            f"node {node_id!r} ({operator.name}) leaves input(s) {', '.join(missing)} unconnected"
        )
    extra = set(node.inputs) - set(operator.inputs)
    if extra:
        raise SpecError(
            f"node {node_id!r} ({operator.name}) connects unknown input(s) {', '.join(sorted(extra))}"
        )

    resolved_inputs = {
        port: (
            reference
            if reference.startswith(SOURCE_PREFIX)
            else structural_hash(reference, nodes, cache)
        )
        for port, reference in node.inputs.items()
    }

    if operator.commutative:
        # Order carries no meaning, so it must not carry a hash either:
        # `and(a, b)` and `and(b, a)` are one strategy, not two trials.
        inputs_payload: Any = sorted(resolved_inputs.values())
    else:
        inputs_payload = resolved_inputs

    digest = content_hash(
        {
            "op": f"{operator.name}@{operator.version}",
            "params": bound,
            "inputs": inputs_payload,
        }
    )
    cache[node_id] = digest
    return digest


def _check_acyclic(nodes: dict[str, Node]) -> None:
    """Depth-first cycle detection with an explicit path, so the error names it."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(nodes, WHITE)

    def visit(node_id: str, path: list[str]) -> None:
        if colour.get(node_id) == GREY:
            cycle = " -> ".join([*path[path.index(node_id) :], node_id])
            raise SpecError(f"spec contains a cycle: {cycle}")
        if colour.get(node_id) == BLACK:
            return
        node = nodes.get(node_id)
        if node is None:
            raise SpecError(f"input references unknown node {node_id!r}")

        colour[node_id] = GREY
        for reference in node.inputs.values():
            if not reference.startswith(SOURCE_PREFIX):
                visit(reference, [*path, node_id])
        colour[node_id] = BLACK

    for node_id in nodes:
        visit(node_id, [])


def _reachable(node_id: str, nodes: dict[str, Node], seen: set[str] | None = None) -> set[str]:
    """Every node in `node_id`'s dependency cone, including itself."""
    seen = set() if seen is None else seen
    if node_id in seen or node_id.startswith(SOURCE_PREFIX):
        return seen
    seen.add(node_id)
    node = nodes.get(node_id)
    if node is None:
        raise SpecError(f"input references unknown node {node_id!r}")
    for reference in node.inputs.values():
        _reachable(reference, nodes, seen)
    return seen


def node_operators(spec: StrategySpec, role: Role) -> set[str]:
    """Ids of every node a role depends on, transitively.

    Transitive on purpose: a spec whose `entry` root is a `crossover` fed by two
    `rolling_mean`s used all three, and a query for *"experiments using
    rolling_mean"* must find it.
    """
    nodes = spec.nodes()
    reachable: set[str] = set()
    for node in spec.roots(role):
        _reachable(node.id, nodes, reachable)
    return reachable


def spec_hash(spec: StrategySpec) -> str:
    """The canonical hash stored on `strategy_specs.spec_hash`."""
    return spec.spec_hash()

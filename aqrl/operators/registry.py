"""The operator registry, and the library version derived from it.

**The library is not self-modifying** (TRD §11.1). New operators may be
proposed, but they enter only via explicit human review with tests —
`operators.approved_by` is NULL until a person signs off, and no agent has a
write path to this module.

### `operator_library_version` is derived, never hand-maintained

TRD §6.6 stamps it on every experiment so that the day the library changes we
can answer *"which of my 40,000 stored results are still comparable?"* from an
index rather than a scan. A hand-maintained version string fails that job the
first time somebody widens a parameter range and forgets to bump it — and the
failure is silent, which is the worst kind: results scored under different rules
quietly mix in the knowledge base, and every cross-experiment lesson A5 draws
from them is noise.

So the version is a content hash over every registered operator's descriptor.
It cannot drift from what it describes.
"""
from __future__ import annotations

from ..hashing import content_hash
from .base import Operator, OperatorError

__all__ = [
    "all_operators",
    "get",
    "names",
    "operator_library_version",
    "register",
    "registry_descriptor",
    "resolve_version",
]

# Keyed by (name, version): several versions of an operator coexist, because an
# old experiment's `spec_hash` must keep meaning exactly what it meant.
_REGISTRY: dict[tuple[str, str], Operator] = {}


def register(cls: type[Operator]) -> type[Operator]:
    """Class decorator. Instantiates once and registers the singleton.

    Re-registering the same `(name, version)` is an error rather than an
    overwrite: a silently replaced operator would change the meaning of every
    stored `spec_hash` that referenced it.
    """
    operator = cls()
    key = (operator.name, operator.version)
    if key in _REGISTRY:
        existing = _REGISTRY[key]
        raise OperatorError(
            f"{operator.name}@{operator.version} is already registered by "
            f"{type(existing).__module__}.{type(existing).__qualname__}. Bump the version rather "
            "than redefining one: stored spec hashes reference the old meaning."
        )
    _REGISTRY[key] = operator
    return cls


def all_operators() -> list[Operator]:
    """Every registered operator, in a stable order."""
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def resolve_version(name: str) -> str:
    """The newest registered version of `name`.

    Used when a spec node leaves `version` unset: the version is pinned **at
    hash time**, so the stored `spec_hash` always names a concrete
    implementation even though the author did not.
    """
    versions = sorted(version for registered, version in _REGISTRY if registered == name)
    if not versions:
        raise OperatorError(f"no operator named {name!r} (have: {', '.join(names()) or 'none'})")
    return versions[-1]


def names() -> list[str]:
    return sorted({name for name, _ in _REGISTRY})


def get(name: str, version: str | None = None) -> Operator:
    """Look one up, defaulting to the newest version."""
    resolved = version or resolve_version(name)
    try:
        return _REGISTRY[(name, resolved)]
    except KeyError:
        available = sorted(v for n, v in _REGISTRY if n == name)
        raise OperatorError(
            f"no operator {name}@{resolved} "
            f"({'versions: ' + ', '.join(available) if available else 'unknown operator'})"
        ) from None


def registry_descriptor() -> list[dict]:
    """Every operator's descriptor — what the library version is taken over."""
    return [operator.descriptor() for operator in all_operators()]


def operator_library_version() -> str:
    """The value stamped into `experiments.operator_library_version`.

    Truncated to 16 hex characters: short enough to read in a table, long
    enough that a collision is not a practical concern for a library of tens of
    operators.
    """
    return content_hash(registry_descriptor())[:16]

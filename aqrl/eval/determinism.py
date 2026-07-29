"""Determinism under parallelism — non-negotiable (TRD §9.5).

> *"The same experiment re-run must produce a **bit-identical** `honest_score`."*

Three rules, and each closes a different way for a parallel run to become
irreproducible:

1. **Seeds are derived, never drawn.** A seed comes from hashing the base seed
   with the labels of the work unit — fold index, train window, replication.
   Never from the wall clock and never from a worker id, because both make the
   result depend on the machine that happened to run it.
2. **Reduction order is fixed.** Floating-point addition is not associative, so
   summing eight folds in completion order gives a different last bit than
   summing them in time order. Everything reduces in a declared order.
3. **Concatenation is chronological.** Fold returns join end to end by date,
   never by whichever process finished first.

A run that violates any of these is not slightly wrong; it is unreproducible,
which means no stored result can ever be re-derived and the archive stops being
evidence.
"""
from __future__ import annotations

import hashlib

import numpy as np

__all__ = ["derive_seed", "rng_for", "stable_sum"]

#: numpy's `default_rng` accepts any non-negative int, but keeping seeds inside
#: 2**32 makes them readable in a log and comparable across languages.
_SEED_MODULUS = 2**32


def derive_seed(base_seed: int, *labels: object) -> int:
    """A reproducible seed for one unit of work.

    Deterministic in `(base_seed, labels)` and nothing else — the same fold of
    the same experiment draws the same numbers on any machine, in any process,
    in any order.
    """
    payload = "|".join([str(base_seed), *(str(label) for label in labels)])
    digest = hashlib.sha256(payload.encode()).digest()
    return int.from_bytes(digest[:8], "big") % _SEED_MODULUS


def rng_for(base_seed: int, *labels: object) -> np.random.Generator:
    return np.random.default_rng(derive_seed(base_seed, *labels))


def stable_sum(values: np.ndarray) -> float:
    """Sum in a fixed order, with pairwise error compensation.

    `math.fsum` would be exact but is a Python loop; `np.add.reduce` uses
    pairwise summation whose result depends only on the array's order, which is
    the property that matters here — the same array always sums to the same
    bits.
    """
    return float(np.add.reduce(np.asarray(values, dtype=float)))

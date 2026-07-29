"""Process-level fan-out — the only kind that helps here (TRD §9.3).

> *"Python threads do not speed up CPU-bound backtesting. The GIL serialises
> them."*

So the unit of parallelism is a **process**, and the level is the **outermost
independent one**: ~24 walk-forward folds × 3 train windows, Monte Carlo
replications, parameter combinations. The inner loop is already vectorised, and
NumPy releases the GIL inside it, so nothing is gained by splitting it further.

**Results come back in submission order, always.** `Executor.map` preserves it;
`as_completed` does not, and using the latter would make fold concatenation
depend on which worker finished first — a bit-level difference in the score
between two runs of the same experiment (TRD §9.5).

Work items must be picklable, which is why the evaluators in this package are
dataclasses over arrays rather than closures. That is a real constraint and it
shapes the design: a closure would be more convenient and would silently
restrict the engine to one core.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from typing import TypeVar

__all__ = ["map_ordered"]

T = TypeVar("T")
R = TypeVar("R")


def map_ordered(
    function: Callable[[T], R],
    items: Sequence[T] | Iterable[T],
    workers: int = 1,
    chunksize: int = 1,
) -> list[R]:
    """`[function(item) for item in items]`, optionally across processes.

    `workers <= 1` runs in this process — not as a fallback but as the default,
    because most units of work here are milliseconds and process startup is not.
    Either path returns results in the order the items were given.
    """
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [function(item) for item in items]

    with ProcessPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(function, items, chunksize=chunksize))

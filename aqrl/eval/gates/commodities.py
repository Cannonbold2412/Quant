"""Commodities gate — roll-method sensitivity (TRD §6.5).

A continuous futures series is a construction, not a fact: it is stitched
across contract expiries by some roll rule (nearest-expiry, volume-triggered,
a fixed calendar day), and a different rule produces a different price series
— sometimes a materially different one, around contango or backwardation.
*"Does this edge survive a different roll method?"* is therefore a real
question distinct from anything the core metrics or the robustness battery ask.

**What is honestly not implemented yet.** Answering it needs the raw
individual-contract history and at least two roll methods applied to the same
underlying — data this repository does not hold (`DataHazards.contract_roll`
exists precisely to flag markets where this matters and the mitigation is not
yet built, the same status the README gives point-in-time equity
membership). Rather than silently skip the question or fabricate a pass, this
gate reports the gap as a `warn` naming exactly what is missing.

The moment a second roll-adjusted series is available for the same instrument,
this becomes a real check: correlate the two return series the same way
`crypto.py`'s venue-robustness check already does, since the shape of the
question — "does this edge survive under a different, legitimate construction
of the same instrument?" — is identical.
"""
from __future__ import annotations

import numpy as np

from ..checks import CheckResult, verdict, warned
from .context import GateContext

__all__ = ["run"]

MIN_ROLL_CORRELATION = 0.5


def run(context: GateContext) -> list[CheckResult]:
    return [_roll_sensitivity(context)]


def _roll_sensitivity(context: GateContext) -> CheckResult:
    alternate = context.alternate_roll_returns
    if alternate is None:
        return warned(
            "commodities_roll_sensitivity",
            "robustness",
            detail=(
                "no alternate roll-method series was supplied for this experiment — "
                "roll-method risk is declared on this market's data hazards but not yet "
                "mitigated or measured (blocked on individual-contract history)"
            ),
        )

    primary = context.result.portfolio_returns
    length = min(primary.size, alternate.size)
    if length < 10:
        return warned(
            "commodities_roll_sensitivity",
            "robustness",
            detail="fewer than 10 overlapping bars between roll methods — not evaluated",
        )

    correlation = float(np.corrcoef(primary[-length:], alternate[-length:])[0, 1])
    return verdict(
        np.isfinite(correlation) and correlation >= MIN_ROLL_CORRELATION,
        "commodities_roll_sensitivity",
        "robustness",
        value=correlation if np.isfinite(correlation) else 0.0,
        threshold=MIN_ROLL_CORRELATION,
    )

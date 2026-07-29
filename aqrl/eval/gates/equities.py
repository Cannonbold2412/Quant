"""Equities gate — survivorship and capacity/ADV (TRD §6.5).

**Survivorship** is already a P0 rejection at the data layer — an unresolved
universe is refused outright, never merely flagged (`p0.py`,
`snapshot_provenance_checks`). This gate's job is narrower: confirm and record
that the mitigation is actually in force for *this* experiment, so the
`evaluation_tests` row for it exists as evidence rather than being implied by
the absence of a P0 failure.

**Capacity/ADV** is the genuinely new check. It asks: *at a plausible capital
size, is the strategy trying to trade more of an instrument's volume than the
market can absorb without moving the price against it?* A backtest is silent
about this by construction — it fills at the quoted price regardless of size —
so a strategy can look excellent purely because its backtest was never told it
would need to trade dollars its own model implies.

**The stated simplification.** There is no assumed-AUM field anywhere in the
schema, so `GateContext.assumed_capital` is a provisional parameter, the same
treatment `bar.py` gives breadth and complexity — measured against a stated
number rather than invented as an unstated one.
"""
from __future__ import annotations

import numpy as np

from ..checks import CheckResult, verdict, warned
from .context import GateContext

__all__ = ["run"]


def run(context: GateContext) -> list[CheckResult]:
    return [_survivorship(context), _capacity(context)]


def _survivorship(context: GateContext) -> CheckResult:
    hazards = context.resolved.market.hazards
    if not hazards.survivorship:
        return warned(
            "equities_survivorship",
            "correctness",
            detail=f"{context.resolved.market.name} does not declare a survivorship hazard",
        )
    return verdict(
        context.survivorship_handled,
        "equities_survivorship",
        "correctness",
        detail=(
            "point-in-time universe resolution is in force for this experiment"
            if context.survivorship_handled
            else "survivorship bias is declared and NOT yet mitigated for this market — "
            "already a P0 rejection, recorded here as confirming evidence"
        ),
    )


def _capacity(context: GateContext) -> CheckResult:
    cap = context.resolved.market.reference.adv_participation_cap_pct
    if cap is None:
        return warned(
            "equities_capacity",
            "performance",
            detail="no ADV participation cap locked for this market — measured, not gated",
        )

    if not context.panel.has("volume"):
        return warned(
            "equities_capacity",
            "performance",
            detail="panel has no volume column — capacity cannot be assessed",
        )

    weights = context.result.weights
    previous = np.zeros_like(weights)
    previous[1:] = weights[:-1]
    traded_fraction = np.abs(weights - previous)

    close = context.panel.column("close")[-weights.shape[0] :]
    volume = context.panel.column("volume")[-weights.shape[0] :]
    dollar_volume = close * volume
    traded_dollars = traded_fraction * context.assumed_capital

    with np.errstate(divide="ignore", invalid="ignore"):
        participation = np.where(dollar_volume > 0.0, traded_dollars / dollar_volume, 0.0)
    worst = float(np.nanmax(participation)) if participation.size else 0.0

    return verdict(
        worst <= cap,
        "equities_capacity",
        "performance",
        value=worst,
        threshold=float(cap),
        detail=f"peak single-bar participation at an assumed capital of {context.assumed_capital:,.0f}",
    )

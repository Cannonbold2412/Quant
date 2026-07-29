"""Crypto gate — venue robustness and funding sensitivity (TRD §6.5).

**Funding sensitivity** is real and computed today: a perpetual's funding
accrues on open exposure regardless of price movement (`costs.funding_costs`),
so a strategy that looks profitable can be one that simply never noticed it
was paying funding every 8 hours. The sweep re-prices funding alone —
isolated from transaction costs — at multiples of the profile's stated rate,
and reports where the edge actually dies.

**Venue robustness** — *"does the edge survive on a second exchange?"* — needs
a second exchange's data, which no market in this repository ships yet. Rather
than fabricate a verdict from one venue's numbers, this reports a `warn` naming
exactly what is missing, the same honest-gap treatment `bar.py` gives breadth
and complexity. When `GateContext.secondary_venue_returns` is supplied, the
check runs for real: the correlation between the two venues' return series is
what the question is actually asking.
"""
from __future__ import annotations

import numpy as np

from ..checks import CheckResult, verdict, warned
from ..costs import funding_costs, transaction_costs
from .context import GateContext

__all__ = ["run"]

#: Multiples of the profile's stated funding rate to sweep through.
FUNDING_STRESS_MULTIPLIERS = (1.0, 2.0, 3.0, 5.0)
#: A venue-robust edge should correlate at least this much with a second venue.
MIN_VENUE_CORRELATION = 0.5


def run(context: GateContext) -> list[CheckResult]:
    return [_funding_sensitivity(context), _venue_robustness(context)]


def _funding_sensitivity(context: GateContext) -> CheckResult:
    cost_model = context.resolved.cost_model
    if not cost_model.funding_rate_bps_per_day:
        return warned(
            "crypto_funding_sensitivity",
            "cost",
            detail=f"{cost_model.asset_class} carries no funding rate on this profile",
        )

    weights = context.result.weights
    bar_seconds = context.resolved.timeframe.bar_seconds

    # Gross price return, net of transaction costs at the run's own multiplier
    # but with funding pulled OUT — so the sweep below varies funding alone,
    # never conflating it with the transaction-cost stress already applied.
    transaction_only = transaction_costs(weights, cost_model, context.result.cost_multiplier)
    base = context.result.gross_returns.sum(axis=1) - transaction_only.sum(axis=1)

    surviving = 0.0
    for multiplier in FUNDING_STRESS_MULTIPLIERS:
        funding = funding_costs(weights, cost_model, bar_seconds, multiplier).sum(axis=1)
        if (base - funding).sum() <= 0.0:
            break
        surviving = multiplier

    return verdict(
        surviving >= FUNDING_STRESS_MULTIPLIERS[0],
        "crypto_funding_sensitivity",
        "cost",
        value=surviving,
        threshold=FUNDING_STRESS_MULTIPLIERS[0],
        detail="largest funding-rate multiplier the strategy still survives",
    )


def _venue_robustness(context: GateContext) -> CheckResult:
    if context.secondary_venue_returns is None:
        return warned(
            "crypto_venue_robustness",
            "robustness",
            detail="no second-venue return series was supplied for this experiment — not evaluated",
        )

    primary = context.result.portfolio_returns
    length = min(primary.size, context.secondary_venue_returns.size)
    if length < 10:
        return warned(
            "crypto_venue_robustness",
            "robustness",
            detail="fewer than 10 overlapping bars between venues — not evaluated",
        )

    correlation = float(
        np.corrcoef(primary[-length:], context.secondary_venue_returns[-length:])[0, 1]
    )
    return verdict(
        np.isfinite(correlation) and correlation >= MIN_VENUE_CORRELATION,
        "crypto_venue_robustness",
        "robustness",
        value=correlation if np.isfinite(correlation) else 0.0,
        threshold=MIN_VENUE_CORRELATION,
    )

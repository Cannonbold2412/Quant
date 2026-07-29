"""Forex gate — session-of-day dependence and carry decomposition (TRD §6.5).

**Session dependence** asks whether an edge is a genuine, roughly
session-independent phenomenon or an artefact of one liquidity window — the
London/New York overlap trades very differently from the Tokyo session, and a
strategy profitable only in one is a narrower discovery than its headline
Sharpe suggests. This is only measurable **below daily** bars, where a session
boundary exists inside a bar's own timestamp; at daily and above there is one
bar per session already and the question collapses to something the regime
analysis already covers, so the check reports that plainly rather than
inventing a session split that is not there.

**Carry** is decomposed from `CostModel.funding_rate_bps_per_day`, the same
field perpetuals use for funding. That is a **stated simplification, not an
oversight**: real FX carry can be a *credit* (the higher-yielding side of a
pair) as well as a cost, but this schema only models the field as something
subtracted (`costs.funding_costs`). The number reported here is therefore *"what
fraction of gross P&L this cost-side term represents"* — informative about how
carry-sensitive the strategy is, not a full carry P&L attribution.
"""
from __future__ import annotations

import numpy as np

from ..checks import CheckResult, passed, verdict, warned
from ..costs import funding_costs
from .context import GateContext

__all__ = ["run"]

SECONDS_PER_DAY = 86_400.0
#: A single session must not carry more than this fraction of total P&L
#: unchallenged — beyond it, the edge reads as session-specific rather than
#: a general phenomenon.
MAX_SESSION_CONCENTRATION = 0.80


def run(context: GateContext) -> list[CheckResult]:
    return [_session_dependence(context), _carry_decomposition(context)]


def _session_dependence(context: GateContext) -> CheckResult:
    bar_seconds = context.resolved.timeframe.bar_seconds
    if bar_seconds >= SECONDS_PER_DAY:
        return warned(
            "forex_session_dependence",
            "regime",
            detail=(
                f"{context.resolved.timeframe.name} bars carry no intraday session "
                "structure — not applicable at this timeframe"
            ),
        )

    returns = context.result.portfolio_returns
    dates = context.result.dates
    seconds_of_day = (
        (dates.astype("datetime64[s]") - dates.astype("datetime64[D]").astype("datetime64[s]"))
        / np.timedelta64(1, "s")
    ).astype(float)
    session_length = SECONDS_PER_DAY / 3.0
    session_index = np.clip((seconds_of_day // session_length).astype(int), 0, 2)

    totals = np.array([returns[session_index == session].sum() for session in range(3)])
    gross_total = float(np.abs(totals).sum())
    concentration = float(np.abs(totals).max() / gross_total) if gross_total > 0.0 else 0.0

    return verdict(
        concentration <= MAX_SESSION_CONCENTRATION,
        "forex_session_dependence",
        "regime",
        value=concentration,
        threshold=MAX_SESSION_CONCENTRATION,
        detail="fraction of total |P&L| earned in the single busiest third-of-day session",
    )


def _carry_decomposition(context: GateContext) -> CheckResult:
    cost_model = context.resolved.cost_model
    if not cost_model.funding_rate_bps_per_day:
        return warned(
            "forex_carry_decomposition",
            "cost",
            detail="this profile declares no carry/funding rate",
        )

    weights = context.result.weights
    bar_seconds = context.resolved.timeframe.bar_seconds
    carry = funding_costs(weights, cost_model, bar_seconds, multiplier=1.0).sum(axis=1)
    gross = context.result.gross_returns.sum(axis=1)

    gross_total = float(np.abs(gross).sum())
    fraction = float(np.abs(carry).sum() / gross_total) if gross_total > 0.0 else 0.0

    return passed(
        "forex_carry_decomposition",
        "cost",
        value=fraction,
        gating=False,
        detail="carry/funding as a fraction of gross |P&L| — informational, not a gate",
    )

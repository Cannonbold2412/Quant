"""Market-specific gates — extra phases **appended**, never a forked engine.

TRD §6.5: *"legitimate per-market variation is extra gates, not extra
engines."* Core metrics, the honest score and the robustness battery are
identical for every market; what genuinely differs is which additional checks
make sense to append after them, and the checks below are exactly that — they
read the same `evaluation_tests` shape as everything else and never touch
`honest_score`.

Selection is by **asset class and declared hazard**, not by market name,
because that is what the docs treat as the first-class distinguishing field
(TRD §6.3) and because it lets these gates be tested against synthetic
profiles today, before a crypto, commodity or forex `MarketProfile` YAML
exists on disk (`Implementation_Plan.md` §21 leaves exactly this open).
"""
from __future__ import annotations

from ..checks import CheckResult
from .context import GateContext
from . import commodities, crypto, equities, forex

__all__ = ["GateContext", "run_market_gates"]


def run_market_gates(context: GateContext) -> list[CheckResult]:
    """Every appended phase applicable to this experiment's profile.

    Multiple gate families can apply at once — a CFD on crypto collateral is
    not in scope today, but nothing here assumes exclusivity.
    """
    checks: list[CheckResult] = []
    asset_class = context.resolved.cost_model.asset_class
    hazards = context.resolved.market.hazards

    if asset_class in ("cash_equity", "etf"):
        checks.extend(equities.run(context))
    if asset_class in ("spot_crypto", "perpetual"):
        checks.extend(crypto.run(context))
    if hazards.contract_roll:
        checks.extend(commodities.run(context))
    if context.is_forex:
        checks.extend(forex.run(context))

    return checks

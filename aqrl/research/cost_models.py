"""Cost models keyed on (market, asset_class), not market alone — TRD §6.3.

Stage 0 held these rates as a hardcoded Python dataclass. Stage 1 moves them
into content-hashed YAML under `profiles/costs/`, so the exact rates a result
was scored under are pinned by `experiments.cost_model_hash` (TRD §6.6). The
day a rate changes, *"which of my stored results are still comparable?"* stays
answerable.

The numbers are unchanged: ~22 bps statutory, ~27.7 bps all-in round trip. So
is the caveat — they are currently-published statutory rates, **not contract-note
figures**, and must be re-derived from a real broker contract note before any
live capital. Stale published rates are a silent systematic bias in every
backtest that uses them.

This module remains the import site `backtest.py` and the tests use; it is now a
loader rather than a definition.
"""

from __future__ import annotations

from aqrl.profiles import CostModel, ProfileError, ProfileLoader

__all__ = ["NSE_CASH_EQUITY", "CostModel", "get_cost_model"]

_loader = ProfileLoader()

# Conservative fallback for a pair with no profile yet. 25 bps per side is
# deliberately punitive: an un-costed market should look expensive, not free.
_PLACEHOLDER_SLIPPAGE_BPS = 25.0


def get_cost_model(market: str, asset_class: str) -> CostModel:
    """Resolve the cost model for a `(market, asset_class)` pair.

    Falls back to a clearly-flagged placeholder rather than raising, so research
    on an un-costed market is possible but never mistakable for a costed one —
    `is_placeholder` travels with the model.
    """
    try:
        return _loader.load_cost_model(market, asset_class)
    except ProfileError:
        return CostModel(
            market=market,
            asset_class=asset_class,  # type: ignore[arg-type]
            version="0.0.0-placeholder",
            currency="INR",
            slippage_bps=_PLACEHOLDER_SLIPPAGE_BPS,
            source="No profile on disk — conservative placeholder",
            requires_contract_note_verification=True,
            is_placeholder=True,
        )


NSE_CASH_EQUITY = get_cost_model("nse_equity", "cash_equity")

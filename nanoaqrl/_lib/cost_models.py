"""Cost models keyed on (market, asset_class), not market alone — TRD §6.3.

NSE cash-equity figures are seeded from real, currently-published statutory
rates (STT, stamp duty, exchange transaction charges, SEBI turnover, GST) —
not guessed. They land at ~22bps statutory / ~27-32bps all-in, matching the
number already locked into the docs (README, TRD §6.3).

**These are defaults, not contract-note figures.** Implementation_Plan's risk
register is explicit: re-derive from a real broker contract note before any
live capital, since stale published rates are a silent systematic bias in
every backtest (Implementation_Plan §18).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    market: str
    asset_class: str
    stt_buy_bps: float
    stt_sell_bps: float
    stamp_duty_buy_bps: float
    stamp_duty_sell_bps: float
    exchange_txn_bps: float  # each side
    sebi_turnover_bps: float  # each side (includes IPFT)
    gst_rate: float  # applied to (brokerage + exchange_txn + sebi), each side
    brokerage_bps: float  # each side
    slippage_bps: float  # each side — market-impact allowance, shrinks with liquidity
    dp_charge_inr: float  # flat, sell side only, equity delivery
    typical_trade_size_inr: float  # only used to express the flat DP charge in bps terms
    is_placeholder: bool = False  # True where the docs mark rates as not yet derived

    def _side_bps(self, stt: float, stamp: float) -> float:
        gst_base = self.brokerage_bps + self.exchange_txn_bps + self.sebi_turnover_bps
        return (
            stt
            + stamp
            + self.exchange_txn_bps
            + self.sebi_turnover_bps
            + self.brokerage_bps
            + self.slippage_bps
            + self.gst_rate * gst_base
        )

    def round_trip_bps(self) -> float:
        """Total cost (bps of notional) for one buy + one sell."""
        buy = self._side_bps(self.stt_buy_bps, self.stamp_duty_buy_bps)
        sell = self._side_bps(self.stt_sell_bps, self.stamp_duty_sell_bps)
        dp_bps = (
            (self.dp_charge_inr / self.typical_trade_size_inr) * 1e4
            if self.typical_trade_size_inr
            else 0.0
        )
        return buy + sell + dp_bps

    def entry_bps(self) -> float:
        return self._side_bps(self.stt_buy_bps, self.stamp_duty_buy_bps)

    def exit_bps(self) -> float:
        dp_bps = (
            (self.dp_charge_inr / self.typical_trade_size_inr) * 1e4
            if self.typical_trade_size_inr
            else 0.0
        )
        return self._side_bps(self.stt_sell_bps, self.stamp_duty_sell_bps) + dp_bps


# STT on equity delivery: 0.1% (=10bps) on BOTH buy and sell.
# Stamp duty: 0.015% (=1.5bps), buy side only, state-levied, SEBI-uniform since 2020.
# Exchange transaction charge (NSE): 0.00297% (=0.297bps) each side.
# SEBI turnover + IPFT: ~0.0001% (=0.01bps) each side.
# Brokerage: 0 — discount-broker delivery trading (e.g. Zerodha) is commonly free.
# Slippage: 2bps each side — a liquid NIFTY-50-name allowance, not a statutory rate.
NSE_CASH_EQUITY = CostModel(
    market="nse_equity",
    asset_class="cash_equity",
    stt_buy_bps=10.0,
    stt_sell_bps=10.0,
    stamp_duty_buy_bps=1.5,
    stamp_duty_sell_bps=0.0,
    exchange_txn_bps=0.297,
    sebi_turnover_bps=0.01,
    gst_rate=0.18,
    brokerage_bps=0.0,
    slippage_bps=2.0,
    dp_charge_inr=15.0,
    typical_trade_size_inr=100_000.0,
)

# Placeholder only — Implementation_Plan §21 lists per-market cost derivation
# for forex/commodities/crypto/futures/CFDs as still open. Conservative and
# clearly flagged rather than fabricated with false precision.
_GENERIC_PLACEHOLDER = CostModel(
    market="*",
    asset_class="*",
    stt_buy_bps=0.0,
    stt_sell_bps=0.0,
    stamp_duty_buy_bps=0.0,
    stamp_duty_sell_bps=0.0,
    exchange_txn_bps=0.0,
    sebi_turnover_bps=0.0,
    gst_rate=0.0,
    brokerage_bps=0.0,
    slippage_bps=25.0,
    dp_charge_inr=0.0,
    typical_trade_size_inr=0.0,
    is_placeholder=True,
)

_REGISTRY: dict[tuple[str, str], CostModel] = {
    (NSE_CASH_EQUITY.market, NSE_CASH_EQUITY.asset_class): NSE_CASH_EQUITY,
}


def get_cost_model(market: str, asset_class: str) -> CostModel:
    key = (market, asset_class)
    if key in _REGISTRY:
        return _REGISTRY[key]
    return CostModel(
        market=market,
        asset_class=asset_class,
        **{
            f: getattr(_GENERIC_PLACEHOLDER, f)
            for f in (
                "stt_buy_bps",
                "stt_sell_bps",
                "stamp_duty_buy_bps",
                "stamp_duty_sell_bps",
                "exchange_txn_bps",
                "sebi_turnover_bps",
                "gst_rate",
                "brokerage_bps",
                "slippage_bps",
                "dp_charge_inr",
                "typical_trade_size_inr",
                "is_placeholder",
            )
        },
    )

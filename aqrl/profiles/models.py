"""Validated profile models — YAML in, content-hashable object out.

Three profile kinds, each answering a different question:

* `MarketProfile`  — what genuinely differs by market (TRD §6.4): calendar,
  constraints, universe, data hazards, reference rates.
* `TimeframeProfile` — what differs by bar size (TRD §13.2), across roughly
  seven orders of magnitude from 1 second to 1 month.
* `CostModel` — keyed on `(market, asset_class)`, never market alone (TRD §6.3).

**Where `periods_per_year` lives, and why not on the timeframe.**
TRD §13.2 lists it as a timeframe field, and warns that *"one wrong value makes
every Sharpe in the database fiction."* But 1-minute bars on NSE and 1-minute
bars on a 24/7 crypto venue do not have the same count — 94,500 against
525,600. A single number on `TimeframeProfile` would therefore have to be wrong
for one of them.

So `TimeframeProfile` carries `bar_seconds`, and `periods_per_year` is
**derived** when a market and a timeframe are resolved together
(`resolve_periods_per_year`). A timeframe may still declare
`periods_per_year_assertions` per market: those are checked at load, which
turns the YAML into a regression test against exactly the mistake TRD warns
about, without pretending the value is market-independent.
"""
from __future__ import annotations

import math
from datetime import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AssetClass = Literal["cash_equity", "etf", "future", "cfd", "spot_crypto", "perpetual"]
FillModel = Literal["next_open", "next_bar_open", "bar_close", "vwap", "queue_position"]
AdjustmentMethod = Literal["back_ratio_price", "back_ratio_total_return", "none"]

PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]

# A calendar year in seconds, for bar sizes at or above one day. 365.25 keeps
# weekly at 52.18 and monthly at exactly 12.
SECONDS_PER_CALENDAR_YEAR = 365.25 * 86_400
SECONDS_PER_DAY = 86_400


class _Frozen(BaseModel):
    """Profiles are immutable: a change is a new version with a new hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Session(_Frozen):
    """One continuous trading window within a day."""

    start: time
    end: time

    @model_validator(mode="after")
    def _ordered(self) -> Session:
        if self.start == self.end:
            raise ValueError("session start and end must differ")
        return self

    @property
    def seconds(self) -> float:
        start = self.start.hour * 3600 + self.start.minute * 60 + self.start.second
        end = self.end.hour * 3600 + self.end.minute * 60 + self.end.second
        # end <= start means the session crosses midnight (MCX evening, forex).
        return float(end - start) if end > start else float(SECONDS_PER_DAY - start + end)


class Calendar(_Frozen):
    """Session hours, holidays, half-days (TRD §6.4).

    NSE 09:15-15:30; MCX to 23:30; crypto 24/7; forex 24/5 with a Sunday open.
    """

    timezone: str
    sessions: list[Session] = Field(min_length=1)
    trading_days_per_year: PositiveFloat
    holidays: list[str] = Field(default_factory=list)
    half_days: dict[str, float] = Field(default_factory=dict)

    @property
    def session_seconds_per_day(self) -> float:
        return sum(session.seconds for session in self.sessions)


class Constraints(_Frozen):
    """Tick size, lot size, margin, circuit limits, short-selling rules."""

    tick_size: PositiveFloat
    lot_size: int = Field(default=1, ge=1)
    contract_multiplier: PositiveFloat = 1.0
    margin_pct: NonNegativeFloat | None = None
    # Circuit limits (India) vs halts (US) — the same field, different regimes.
    circuit_limit_pct: NonNegativeFloat | None = None
    # Indian cash equities: intraday only.
    short_selling: Literal["allowed", "intraday_only", "forbidden"] = "allowed"


class Universe(_Frozen):
    """How the instrument set is determined on each bar."""

    # When set, membership resolves point-in-time from `index_membership`.
    index_name: str | None = None
    instruments: list[str] = Field(default_factory=list)
    # False until price history for every ever-member exists (TRD §14.5).
    point_in_time_available: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> Universe:
        if not self.index_name and not self.instruments:
            raise ValueError("universe needs either an index_name or an explicit instrument list")
        return self


class DataHazards(_Frozen):
    """What is known to be wrong with this market's data (TRD §6.4)."""

    survivorship: bool = False
    contract_roll: bool = False
    exchange_bad_prints: bool = False
    unadjusted_prices: bool = False
    notes: str | None = None


class Reference(_Frozen):
    """Risk-free rate source, benchmark, P&L currency, liquidity cap."""

    currency: str = Field(min_length=3, max_length=3)
    risk_free_rate_source: str | None = None
    benchmark: str | None = None
    adv_participation_cap_pct: NonNegativeFloat | None = None


class MarketProfile(_Frozen):
    name: str
    version: str
    description: str | None = None
    asset_classes: list[AssetClass] = Field(min_length=1)
    calendar: Calendar
    constraints: Constraints
    universe: Universe
    reference: Reference
    hazards: DataHazards = DataHazards()
    # Per-market override of the bar's drawdown ceiling: 0.15 default, 0.20 crypto.
    max_drawdown_override: NonNegativeFloat | None = None
    # |return| above this with no matching corporate action raises an
    # `unexplained_jump` flag. Profile-driven because a 20% move means something
    # different on a circuit-limited equity than on spot crypto.
    unexplained_jump_threshold: PositiveFloat = 0.20


class WalkForwardWindows(_Frozen):
    """Windows sized in BOTH bars and calendar time (TRD §13.2).

    Bars give statistical power; calendar time gives regime coverage. At
    1-second a 1-year test window is ~5.7M bars; at monthly it is 12.
    """

    train_years: list[int] = Field(default=[1, 2, 3])
    test_years: int = 1
    min_train_bars: int = Field(gt=0)
    min_test_bars: int = Field(gt=0)


class TimeframeProfile(_Frozen):
    name: str
    version: str
    description: str | None = None
    # 1 second to 1 month — roughly seven orders of magnitude.
    bar_seconds: PositiveFloat
    fill_model: FillModel
    # At 1-second costs and spread dominate; at monthly they are a rounding error.
    cost_stress_multipliers: list[PositiveFloat] = Field(default=[1.0, 2.0])
    walk_forward: WalkForwardWindows
    min_trades: int = Field(default=100, gt=0)
    overnight_positions: bool = True
    # Below ~1 minute, fills depend on queue position and latency that bar data
    # cannot represent. Supported is not the same as trustworthy (TRD §13.2).
    fidelity_warning: str | None = None
    # Optional per-market regression guards on the derived periods_per_year.
    periods_per_year_assertions: dict[str, PositiveFloat] = Field(default_factory=dict)

    @field_validator("bar_seconds")
    @classmethod
    def _within_supported_range(cls, value: float) -> float:
        if value < 1.0:
            raise ValueError("bar_seconds below 1 second is outside the supported range")
        if value > SECONDS_PER_CALENDAR_YEAR / 12 * 1.05:
            raise ValueError("bar_seconds above one month is outside the supported range")
        return value

    @model_validator(mode="after")
    def _sub_minute_is_flagged(self) -> TimeframeProfile:
        if self.bar_seconds < 60 and not self.fidelity_warning:
            raise ValueError(
                "sub-minute timeframes must carry an explicit fidelity_warning: below ~1 minute "
                "backtest realism degrades sharply and the architecture supporting it is not the "
                "same as the results being trustworthy (TRD §13.2)"
            )
        return self


class CostModel(_Frozen):
    """Costs for one `(market, asset_class)` pair.

    NSE cash delivery works out to ~22 bps statutory, ~27-32 bps all-in, which
    is what makes a swing strategy need >30 bps per round trip merely to break
    even — and ~55-65 bps to survive the 2x cost stress.

    `source` and `requires_contract_note_verification` are not decoration. The
    risk register is explicit that published rates go stale and differ by
    segment, and that a cost model sourced from a blog is *"a silent, systematic
    bias in every backtest that uses it."*
    """

    market: str
    asset_class: AssetClass
    version: str
    currency: str = Field(min_length=3, max_length=3)

    stt_buy_bps: NonNegativeFloat = 0.0
    stt_sell_bps: NonNegativeFloat = 0.0
    stamp_duty_buy_bps: NonNegativeFloat = 0.0
    stamp_duty_sell_bps: NonNegativeFloat = 0.0
    exchange_txn_bps: NonNegativeFloat = 0.0
    sebi_turnover_bps: NonNegativeFloat = 0.0
    gst_rate: NonNegativeFloat = 0.0
    brokerage_bps: NonNegativeFloat = 0.0
    slippage_bps: NonNegativeFloat = 0.0
    # Flat charges (e.g. NSE DP charge on delivery sells) expressed in bps
    # against a reference trade size.
    flat_charge_sell: NonNegativeFloat = 0.0
    reference_trade_size: NonNegativeFloat = 0.0
    # Crypto perpetuals: funding accrues while a position is open.
    funding_rate_bps_per_day: NonNegativeFloat = 0.0

    source: str
    derived_at: str | None = None
    # True until re-derived from a real broker contract note.
    requires_contract_note_verification: bool = True
    is_placeholder: bool = False

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

    @property
    def _flat_sell_bps(self) -> float:
        if not self.reference_trade_size:
            return 0.0
        return (self.flat_charge_sell / self.reference_trade_size) * 1e4

    def entry_bps(self) -> float:
        return self._side_bps(self.stt_buy_bps, self.stamp_duty_buy_bps)

    def exit_bps(self) -> float:
        return self._side_bps(self.stt_sell_bps, self.stamp_duty_sell_bps) + self._flat_sell_bps

    def round_trip_bps(self) -> float:
        """Total cost in bps of notional for one buy plus one sell."""
        return self.entry_bps() + self.exit_bps()


class ResolvedProfile(_Frozen):
    """A market and a timeframe resolved together, with the cost model.

    This is the unit an experiment pins. The three hashes plus
    `periods_per_year` are what TRD §6.6 requires stamped on every result, and
    what lets us answer *"which of my 40,000 stored results are still
    comparable?"* the day any of them changes.
    """

    market: MarketProfile
    timeframe: TimeframeProfile
    cost_model: CostModel
    periods_per_year: PositiveFloat
    market_profile_hash: str
    timeframe_profile_hash: str
    cost_model_hash: str

    @property
    def annualisation_factor(self) -> float:
        """sqrt(periods_per_year) — the Sharpe scaler. Never a constant."""
        return math.sqrt(self.periods_per_year)

    @property
    def max_drawdown_limit(self) -> float | None:
        return self.market.max_drawdown_override


def resolve_periods_per_year(market: MarketProfile, timeframe: TimeframeProfile) -> float:
    """Derive bars per year from the market calendar and the bar size.

    Three regimes, because a bar smaller than a session and a bar larger than a
    day are counted differently:

    * **Intraday** — trading days x (session seconds / bar seconds).
      NSE 09:15-15:30 is 22,500s, so 1-minute bars give 252 x 375 = 94,500 and
      1-second bars give 5,670,000.
    * **Daily** — the calendar's trading days, 252 for NSE, 365 for crypto.
    * **Multi-day** — calendar arithmetic: weekly 52.18, monthly 12.
    """
    session_seconds = market.calendar.session_seconds_per_day
    trading_days = market.calendar.trading_days_per_year
    bar_seconds = timeframe.bar_seconds

    if bar_seconds < session_seconds:
        return trading_days * (session_seconds / bar_seconds)
    if bar_seconds <= SECONDS_PER_DAY:
        # One bar per trading day, whatever fraction of the day it covers.
        return trading_days
    return SECONDS_PER_CALENDAR_YEAR / bar_seconds


def check_periods_per_year(market: MarketProfile, timeframe: TimeframeProfile) -> float:
    """Derive, and verify any assertion the timeframe declared for this market."""
    derived = resolve_periods_per_year(market, timeframe)
    asserted = timeframe.periods_per_year_assertions.get(market.name)
    if asserted is not None and not math.isclose(derived, asserted, rel_tol=1e-3):
        raise ValueError(
            f"periods_per_year mismatch for {market.name} x {timeframe.name}: "
            f"profile asserts {asserted}, calendar derives {derived:.4f}. "
            "One wrong value makes every Sharpe in the database fiction (TRD §13.2)."
        )
    return derived


def profile_payload(profile: BaseModel) -> dict[str, Any]:
    """The dict a profile's content hash is taken over."""
    return profile.model_dump(mode="json")

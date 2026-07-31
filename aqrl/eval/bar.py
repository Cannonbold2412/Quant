"""The hard bar — pre-registered, pass/fail, and enforced here.

> *"`program.md` is instructions — the agent decides whether it complied, and
> will eventually persuade itself that 40 trades is close enough to 100. The bar
> therefore lives in both files with different jobs: `program.md` states the
> target; `evaluate.py` **enforces** it."* (TRD §7.5)

**Failing the bar means no score is computed at all.** Not "computed and
ignored" — the loop must not be able to see how close it came, because a
gradient towards the bar is a gradient towards gaming it.

**Where each item is checked.** App-Flow §5 draws the bar as the first box in
the funnel; TRD §7.5 requires a bar failure to precede any scoring. Both hold,
because the items divide cleanly:

| Item | Checked | Why there |
|---|---|---|
| complexity | before P0 | A property of the spec. Needs no data and no compute. |
| min trades · max drawdown · breadth · 2× cost survival | the moment the concatenated out-of-sample series exists, **before** the score | They need returns, and nothing else. |
| minimum honest score | immediately after the score | It *is* the score. |

The four data-dependent items are the ones the `evaluations.bar_failed_on` enum
names. A score below the minimum is recorded through `failure_reason =
'deflated_sharpe_insufficient'`, which is what that value means.

**Drawdown gates but does not rank** (TRD §7.5). Max drawdown is one noisy
worst-moment statistic; as a gate its noisiness is harmless, as a ranking it is
corrosive, which is why it appears here and never in the score.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..operators.spec import StrategySpec
from ..profiles.models import MarketProfile
from .checks import CheckResult, verdict, warned

__all__ = ["AcceptanceBar", "BarVerdict", "BAR_FAILURE_TO_EXPERIMENT_REASON", "breadth_of", "complexity_of"]

#: TRD §7.5. These are the defaults a campaign inherits when no `acceptance_bars`
#: row has been locked; a locked row always wins, because pre-registration is
#: the point.
DEFAULT_MIN_SCORE = 0.50
DEFAULT_MAX_DRAWDOWN = 0.15
DEFAULT_MIN_TRADES = 100
DEFAULT_COST_STRESS = 2.0
DEFAULT_Z_MULTIPLIER = 1.65

#: The order failures are reported in — cheapest and most diagnostic first.
_FAILURE_ORDER = ("complexity", "min_trades", "max_drawdown", "cost_stress", "breadth")

#: `BarVerdict.failed_on`'s vocabulary (this module's, matching
#: `evaluations.bar_failed_on`) is not `experiments.failure_reason`'s vocabulary
#: (Backend-Schema §6) — the two enums were never meant to be the same string.
#: `engine.py` needs this map to close out a bar-failed experiment at all: an
#: un-translated `"min_trades"` written straight to `experiments.failure_reason`
#: violates that column's own CHECK constraint. `min_trades` -> `insufficient_trades`
#: is exact; `cost_stress` -> `costs_exceed_edge` reuses the same bucket P2's own
#: cost-survival failure already uses, since both ask the identical question at
#: different phases. `max_drawdown` and `breadth` have no dedicated slot in the
#: schema, so each takes the closest existing bucket — the same "closest
#: available bucket" tradeoff `implement.py`'s P0-provenance handling and A2's
#: FIX_CODE quarantine reason already make, not a new precedent.
BAR_FAILURE_TO_EXPERIMENT_REASON: dict[str, str] = {
    "min_trades": "insufficient_trades",
    "cost_stress": "costs_exceed_edge",
    "max_drawdown": "monte_carlo_ruin_risk",
    "breadth": "capacity_constrained",
}


@dataclass(frozen=True)
class AcceptanceBar:
    """The pre-registered pass/fail bar for one campaign."""

    min_score: float = DEFAULT_MIN_SCORE
    max_drawdown: float = DEFAULT_MAX_DRAWDOWN
    min_trades: int = DEFAULT_MIN_TRADES
    #: Open questions, human-owned (`Implementation_Plan.md` §21). `None` means
    #: measured and recorded but not gated — the engine reports the number and
    #: declines to invent a threshold for it.
    min_breadth: float | None = None
    max_complexity: int | None = None
    cost_stress_multiple: float = DEFAULT_COST_STRESS
    z_multiplier: float = DEFAULT_Z_MULTIPLIER
    plateau_patience: int = 5
    hard_iteration_cap: int = 25
    campaign_label: str | None = None

    # -- construction ----------------------------------------------------------

    def for_market(self, market: MarketProfile) -> AcceptanceBar:
        """Apply the market's drawdown override — 15% default, 20% for crypto."""
        if market.max_drawdown_override is None:
            return self
        return AcceptanceBar(**{**self.__dict__, "max_drawdown": market.max_drawdown_override})

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AcceptanceBar:
        return cls(
            min_score=float(row["min_score"]),
            max_drawdown=float(row["max_drawdown"]),
            min_trades=int(row["min_trades"]),
            min_breadth=None if row.get("min_breadth") is None else float(row["min_breadth"]),
            max_complexity=None
            if row.get("max_complexity") is None
            else int(row["max_complexity"]),
            cost_stress_multiple=float(row["cost_stress_multiple"]),
            z_multiplier=float(row["z_multiplier"]),
            plateau_patience=int(row.get("plateau_patience", 5)),
            hard_iteration_cap=int(row.get("hard_iteration_cap", 25)),
            campaign_label=row.get("campaign_label"),
        )

    @classmethod
    def locked(cls, conn: sqlite3.Connection, campaign_label: str | None = None) -> AcceptanceBar:
        """The campaign's locked bar, or the TRD defaults when none is locked.

        A superseded row is never used: changing a bar mid-campaign creates a
        new row precisely so the old one stops applying.
        """
        sql = "SELECT * FROM acceptance_bars WHERE superseded_by IS NULL"
        params: list[Any] = []
        if campaign_label is not None:
            sql += " AND campaign_label = ?"
            params.append(campaign_label)
        sql += " ORDER BY id DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        return cls.from_row(dict(row)) if row is not None else cls(campaign_label=campaign_label)


@dataclass(frozen=True)
class BarVerdict:
    """The outcome of the data-dependent half of the bar."""

    passed: bool
    failed_on: str | None
    checks: list[CheckResult]

    @property
    def blocks_scoring(self) -> bool:
        return not self.passed


def complexity_of(spec: StrategySpec) -> int:
    """Node count of the reachable DAG — the provisional complexity measure.

    Counts what the strategy *computes*, so two spellings of the same DAG score
    identically, and an unused node costs nothing. How complexity should really
    be measured is an open question owned by a human; this number is recorded
    with its threshold visible so the eventual answer can be checked against
    what was actually being counted.
    """
    return len(spec.nodes())


def breadth_of(instrument_pnl: np.ndarray, instrument_trades: np.ndarray) -> float:
    """Fraction of traded instruments that contributed positively.

    The provisional definition, for the same reason as `complexity_of`. It asks
    the question breadth exists to ask — *is this edge present in more than one
    instrument, or is it one lucky name carrying the portfolio?* — while leaving
    the threshold to whoever locks the campaign.
    """
    traded = instrument_trades > 0
    if not traded.any():
        return 0.0
    return float((instrument_pnl[traded] > 0).sum() / traded.sum())


def check_complexity(spec: StrategySpec, bar: AcceptanceBar) -> CheckResult:
    """The one bar item checkable before any data is touched."""
    value = float(complexity_of(spec))
    if bar.max_complexity is None:
        return warned(
            "complexity",
            "correctness",
            value=value,
            detail="no complexity cap locked for this campaign — measured, not gated",
        )
    return verdict(
        value <= bar.max_complexity,
        "complexity",
        "correctness",
        value=value,
        threshold=float(bar.max_complexity),
    )


def check_outcome(
    bar: AcceptanceBar,
    *,
    n_trades: int,
    max_drawdown: float,
    oos_return_total: float,
    breadth: float,
) -> BarVerdict:
    """The data-dependent items, run before any score exists.

    `oos_return_total` is the sum of the concatenated out-of-sample series,
    which is already computed at the stress multiplier — cost survival is the
    default condition of the scored series, not a separate later test (TRD §7.2).
    """
    checks = [
        verdict(
            n_trades >= bar.min_trades,
            "min_trades",
            "performance",
            value=float(n_trades),
            threshold=float(bar.min_trades),
        ),
        verdict(
            max_drawdown <= bar.max_drawdown,
            "max_drawdown",
            "performance",
            value=float(max_drawdown),
            threshold=float(bar.max_drawdown),
        ),
        verdict(
            oos_return_total > 0.0,
            "cost_stress",
            "cost",
            value=float(oos_return_total),
            threshold=0.0,
            detail=f"out-of-sample total return at {bar.cost_stress_multiple}× costs",
        ),
    ]

    if bar.min_breadth is None:
        checks.append(
            warned(
                "breadth",
                "performance",
                value=breadth,
                detail="no breadth floor locked for this campaign — measured, not gated",
            )
        )
    else:
        checks.append(
            verdict(
                breadth >= bar.min_breadth,
                "breadth",
                "performance",
                value=breadth,
                threshold=bar.min_breadth,
            )
        )

    blocking = [check.test_name for check in checks if check.blocking]
    failed_on = next((name for name in _FAILURE_ORDER if name in blocking), None)
    return BarVerdict(passed=not blocking, failed_on=failed_on, checks=checks)


def check_min_score(bar: AcceptanceBar, honest_score: float) -> CheckResult:
    """The last bar item, and the only one that needs the score to exist."""
    return verdict(
        honest_score >= bar.min_score,
        "min_honest_score",
        "robustness",
        value=float(honest_score),
        threshold=float(bar.min_score),
    )

"""The `MONITOR_DEPLOYMENT` handler — Stage 11 (Implementation_Plan §14).

**Trigger:** `scheduler.fire_due_monitor_batch`, once per active deployment,
per day — not `Event.PAPER_TRADING_MILESTONE` (declared in `events.py` but
deliberately left unfired here; its mapping is `-> MONITOR_DEPLOYMENT`
itself, so firing it would make this handler re-enqueue its own job type).
The daily fan-out is the real producer, the relationship
`fire_due_hypothesis_batch` already has to A1's nightly batch.

**Same `run`/`persist` split as every other handler**, and the same stale
guard `promote.py` established: a deployment no longer `active` by the time
this job is claimed (already stopped or retired) is a no-op, not an error —
as is a deployment with nothing to replay against yet (no ingested snapshot).

**Risk limits are enforced independently of the health verdict** (TRD §18 —
"hard risk limits enforced outside strategy logic"). A kill-switch trip
stops the deployment and discards every trade at or after the breach bar
regardless of what `health_verdict` says; the promotion gate is never
checked on a breached run, so a strategy can never be gated to live in the
same check that just kill-switched it.

**The promotion row is written directly here, with no LLM call** — the same
"human-authored, no re-run" shape `gates.reject` uses for a rejection
lesson. App-Flow §10 says "enqueue PROMOTE", but `promote.py` is A4, the
research committee, and its brief reasons about validation evidence and the
robustness battery — not forward paper-trading results. Re-running it here
would ask the wrong agent the wrong question; PRD §9.3's five conditions are
a mechanical check, not a judgement call.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ... import monitoring
from ...config import get_settings
from ...db.repositories import (
    DeploymentRepository,
    HealthCheckRepository,
    LifecycleEventRepository,
    PromotionRepository,
    ResearchQuestionRepository,
    StrategyRepository,
    TradeRepository,
)
from ...db.repositories.base import Row, utcnow_iso
from ...monitoring import GateResult, HealthVerdict, ReplayResult
from ...profiles.models import ResolvedProfile
from ..events import Event, emit
from ..states import transition
from .base import HandlerResult

__all__ = ["MonitorOutcome", "persist", "run"]


@dataclass(frozen=True)
class MonitorOutcome:
    stale: bool
    deployment_id: int
    no_data: bool = False
    replay: ReplayResult | None = None
    verdict: HealthVerdict | None = None
    gate: GateResult | None = None
    breach_index: int | None = None


def run(conn: sqlite3.Connection, job: Row) -> MonitorOutcome:
    """The heavy, side-effect-free-on-the-database half. Writes nothing —
    every insert and state transition happens in `persist()`."""
    payload = job["payload"] or {}
    deployment_id = payload.get("deployment_id")
    if deployment_id is None:
        raise ValueError("MONITOR_DEPLOYMENT job has no deployment_id in payload")

    deployment = DeploymentRepository(conn).get(deployment_id)
    if deployment is None:
        raise ValueError(f"no deployment {deployment_id}")

    # Stale guard, same shape as `promote.py`: a deployment already stopped
    # or retired by a prior check (or a human) is a no-op, not an error.
    if deployment["status"] != "active":
        return MonitorOutcome(stale=True, deployment_id=deployment_id)

    result = monitoring.replay(conn, deployment)
    if result is None:
        # No snapshot ingested yet for this (market, timeframe, asset_class)
        # — an expected condition before `COLLECT_MARKET_DATA` has run, not
        # a failure.
        return MonitorOutcome(stale=True, deployment_id=deployment_id, no_data=True)

    settings = get_settings()
    breach_index = monitoring.risk_breach(result.paper_result.equity, settings)
    verdict = monitoring.health_verdict(result, deployment, settings)
    gate = monitoring.promotion_gate(deployment, verdict)

    return MonitorOutcome(
        stale=False,
        deployment_id=deployment_id,
        replay=result,
        verdict=verdict,
        gate=gate,
        breach_index=breach_index,
    )


def _trade_row(deployment: Row, trade, resolved: ResolvedProfile, regime: str | None) -> dict:
    """One `trades` row from a replayed `Trade`.

    `entry_price`/`exit_price` stay NULL — a replay works in returns, never
    prices, so there is nothing honest to put there. `quantity` carries
    `avg_weight` (fraction of equity), the closest analogue a fractional,
    weight-based backtest has to a share count. `execution_quality` is
    always `'good'` and `actual_slippage_bps` always equals
    `expected_slippage_bps` — both derive from the same `cost_model.
    slippage_bps` by construction in a snapshot-replay simulation; this
    dimension only becomes real against a broker (a known limit, not
    papered over with invented jitter).
    """
    capital = deployment["capital_minor_units"] or 0
    slippage_bps = resolved.cost_model.slippage_bps
    return dict(
        deployment_id=deployment["id"],
        instrument=trade.instrument,
        side="long" if trade.side > 0 else "short",
        entry_time=trade.entry_date,
        exit_time=trade.exit_date,
        quantity=trade.avg_weight,
        pnl_minor_units=int(round(trade.net_return * capital)),
        currency=deployment["currency"],
        fees_minor_units=int(round(trade.cost * capital)),
        expected_slippage_bps=slippage_bps,
        actual_slippage_bps=slippage_bps,
        execution_quality="good",
        regime_at_entry=regime,
        signal_reference=None,
    )


def persist(conn: sqlite3.Connection, job: Row, outcome: MonitorOutcome) -> HandlerResult:
    """The short, atomic half: every database write, in one transaction."""
    if outcome.stale:
        return HandlerResult(tokens_spent=0)

    deployments = DeploymentRepository(conn)
    deployment = deployments.get(outcome.deployment_id)
    if deployment is None:
        raise ValueError(f"no deployment {outcome.deployment_id}")

    result = outcome.replay
    verdict = outcome.verdict
    gate = outcome.gate
    assert result is not None and verdict is not None and gate is not None  # narrows for the type checker

    # -- record new paper trades, idempotently ---------------------------------
    trades_repo = TradeRepository(conn)
    existing_keys = trades_repo.existing_keys(deployment["id"])
    # A risk breach discards every trade at or after the breach bar — never
    # recorded, not merely flagged (TRD §18).
    cutoff_date = str(result.paper_result.dates[outcome.breach_index]) if outcome.breach_index is not None else None

    recorded = 0
    regimes_seen: set[str] = set()
    for trade in result.paper_trades:
        if cutoff_date is not None and trade.entry_date >= cutoff_date:
            continue
        key = (trade.instrument, trade.entry_date)
        if key in existing_keys:
            continue
        regime = result.regime_by_date.get(trade.entry_date)
        trades_repo.insert(**_trade_row(deployment, trade, result.resolved, regime))
        recorded += 1
        if regime is not None:
            regimes_seen.add(regime)

    if recorded:
        deployments.record_trade_progress(deployment["id"], new_trades=recorded, regimes_seen=sorted(regimes_seen))

    # -- the health_checks row --------------------------------------------------
    health_checks = HealthCheckRepository(conn)
    previous = health_checks.latest_for_deployment(deployment["id"])
    health_checks.insert(
        deployment_id=deployment["id"],
        check_time=utcnow_iso(),
        level=verdict.level,
        live_sharpe=verdict.live_sharpe,
        live_max_dd=verdict.live_max_dd,
        live_win_rate=verdict.live_win_rate,
        live_profit_factor=verdict.live_profit_factor,
        sharpe_zscore=verdict.sharpe_zscore,
        win_rate_zscore=verdict.win_rate_zscore,
        avg_trade_zscore=verdict.avg_trade_zscore,
        loss_distribution_pvalue=verdict.loss_distribution_pvalue,
        current_regime=verdict.current_regime,
        regime_historically_weak=verdict.regime_historically_weak,
        slippage_deviation=verdict.slippage_deviation,
        missed_fill_rate=verdict.missed_fill_rate,
        verdict_reason=verdict.verdict_reason,
        recommended_action=verdict.recommended_action,
    )
    deployments.update(deployment["id"], current_health=verdict.level)

    if previous is None or previous["level"] != verdict.level:
        LifecycleEventRepository(conn).insert(
            deployment_id=deployment["id"],
            strategy_id=deployment["strategy_id"],
            event_type="health_change",
            from_state=previous["level"] if previous else None,
            to_state=verdict.level,
            triggered_by="automatic_rule",
            reason=verdict.verdict_reason,
        )

    if verdict.level == "red":
        deployments.update(deployment["id"], status="stopped", ended_at=utcnow_iso())
        emit(
            conn,
            Event.HEALTH_CHECK_RED,
            strategy_id=deployment["strategy_id"],
            payload={"deployment_id": deployment["id"], "verdict_reason": verdict.verdict_reason},
            reasoning=verdict.verdict_reason,
        )
        ResearchQuestionRepository(conn).push(
            f"Deployment {deployment['id']} went red: {verdict.verdict_reason}. What changed?",
            motivation=verdict.verdict_reason,
            origin_type="live_degradation",
            origin_experiment_id=deployment.get("experiment_id"),
        )

    # -- hard risk limits: independent of the health verdict (TRD §18) ---------
    if outcome.breach_index is not None:
        deployments.update(deployment["id"], status="stopped", ended_at=utcnow_iso())
        LifecycleEventRepository(conn).insert(
            deployment_id=deployment["id"],
            strategy_id=deployment["strategy_id"],
            event_type="kill_switch",
            from_state="active",
            to_state="stopped",
            triggered_by="automatic_rule",
            reason=f"hard risk limit breached at paper-era bar {outcome.breach_index}",
        )
    elif gate.passed:
        # Re-fetch: trade progress recorded above may have just crossed
        # `trades_required` this run.
        refreshed = deployments.get(deployment["id"])
        if refreshed is None:
            raise ValueError(f"deployment {deployment['id']} vanished mid-persist")
        deployment = refreshed
        strategy = StrategyRepository(conn).get(deployment["strategy_id"])
        # Idempotency guard: only the first run to clear the gate writes a
        # promotion row — every later `active` check that still passes
        # would otherwise write one every day the human has yet to act.
        if strategy is not None and strategy["status"] == "paper_trading":
            PromotionRepository(conn).insert(
                strategy_id=strategy["id"],
                best_experiment_id=deployment.get("experiment_id"),
                stage_from="paper",
                stage_to="live_small",
                decision="approve",
                rationale=(
                    "Paper-trading promotion gate cleared (PRD §9.3): trade count, deviation from "
                    "validation, regime coverage, execution quality and health checks all passed."
                ),
                evidence_summary={
                    "trades_completed": deployment["trades_completed"],
                    "trades_required": deployment["trades_required"],
                    "regimes_observed": deployment["regimes_observed"],
                    "health_level": verdict.level,
                },
                requires_human_approval=True,
                human_decision="pending",
            )
            transition(
                conn,
                "strategies",
                strategy["id"],
                "pending_live_review",
                actor="system:monitor",
                reasoning="paper-trading promotion gate cleared (PRD §9.3)",
            )

    return HandlerResult(tokens_spent=0)

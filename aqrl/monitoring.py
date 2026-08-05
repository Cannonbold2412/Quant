"""Stage 11 — Paper Trading & Health Monitoring (Implementation_Plan §14).

Holds Stage 11's whole logic layer, the way `gates.py` held Stage 9's — the
`MONITOR_DEPLOYMENT` handler (`orchestration/handlers/monitor.py`) is a thin
`run`/`persist` shell around the automatic half (`replay`/`risk_breach`/
`health_verdict`/`promotion_gate`), and `kill`/`retire` below are this
module's human-triggered half — the same split `gates.py` has between
`promote.py`'s automatic A4 proposal and `gates.approve`/`reject`'s human
writes.

**The paper executor is snapshot replay, not a broker.** There is no live
feed and no credentials in this codebase, and live broker integration is
explicitly deferred until M7 (Implementation_Plan §20). Replaying the
validated engine over the newest ingested snapshot answers the forward-
evidence question without a second execution path that could drift from the
one `evaluate.py` already validated against (TRD §6.1's "exactly one
implementation" rule, extended here the way Stage 5 extended it to codegen).

**The core idea: one backtest, split into two eras at `deployments.
started_at`.** Slicing the panel to forward bars first would be wrong —
indicators need warm-up history, so a sliced panel computes different
signals than the strategy actually generates. Instead the full panel is
backtested once, and the result is split by date after the fact:

    baseline era: entry_date <= started_at   -> the yardstick
    paper era:    entry_date >  started_at   -> forward evidence

The paper era's bars did not exist at validation time, so this is genuine
forward evidence, growing on its own as new snapshots are ingested (TRD
§14 — `COLLECT_MARKET_DATA`), with no live feed required.

**Z-scores need a spread, and `deployments.expected_*` stores point values
only.** The point estimate (the numerator) is `deployments.expected_*` — the
value the human actually approved at Gate 1, never recomputed here, so a
later replay can never silently drift from what was shown at approval. The
*spread* (the denominator) is derived fresh from this replay's own baseline
era each time, since Backend-Schema never stored one.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
from scipy import stats as scipy_stats

from .agents.render import code_path_for
from .config import Settings, get_settings
from .data.snapshots import SnapshotManager
from .db import transaction
from .db.repositories import (
    DeploymentRepository,
    ExperimentRepository,
    KnowledgeEntryRepository,
    LifecycleEventRepository,
    SnapshotRepository,
    SpecRepository,
    StrategyRepository,
)
from .db.repositories.base import Row, utcnow_iso
from .eval.backtest import BacktestResult, run_backtest
from .eval.engine import full_signals
from .eval.metrics import CoreMetrics, compute_metrics
from .eval.panel import PricePanel
from .eval.regimes import label_regimes, market_returns, regime_performance
from .eval.stats.honest_score import se_sharpe
from .eval.tradebook import Trade, closed_trades, extract_trades
from .operators.compile import compile_spec
from .orchestration.states import transition
from .profiles import ProfileLoader
from .profiles.models import ResolvedProfile

__all__ = [
    "GateResult",
    "HealthVerdict",
    "ReplayResult",
    "health_verdict",
    "kill",
    "promotion_gate",
    "replay",
    "retire",
    "risk_breach",
]

#: The multiplier every prior stage defaults to for a "realistic" (non-
#: stress) backtest — `EvaluationInputs.cost_multiplier`'s own default and
#: `handlers/evaluate.py`'s payload fallback. Not recoverable per-experiment
#: (neither `experiments` nor `evaluations` persists the multiplier a given
#: result was computed at), so replay uses the same system-wide default
#: rather than inventing per-deployment provenance that does not exist.
DEFAULT_COST_MULTIPLIER = 2.0

@dataclass(frozen=True)
class ReplayResult:
    """One full-panel backtest, split at `deployments.started_at`."""

    resolved: ResolvedProfile
    baseline_trades: list[Trade]
    paper_trades: list[Trade]
    baseline_result: BacktestResult
    paper_result: BacktestResult
    #: Regimes where this strategy's OWN baseline-era Sharpe was negative —
    #: what PRD §9.4's "a regime it historically struggled in" means,
    #: measured rather than assumed.
    weak_regimes: frozenset[str]
    current_regime: str | None
    #: Bar-date -> regime label, over the whole replayed panel — what the
    #: handler reads to fill `trades.regime_at_entry` without re-running
    #: `label_regimes` a second time outside this module.
    regime_by_date: dict[str, str]


@dataclass(frozen=True)
class HealthVerdict:
    """Every `health_checks` column, computed."""

    level: str  # green | yellow | orange | red
    live_sharpe: float
    live_max_dd: float
    live_win_rate: float
    live_profit_factor: float
    sharpe_zscore: float | None
    win_rate_zscore: float | None
    avg_trade_zscore: float | None
    loss_distribution_pvalue: float | None
    current_regime: str | None
    regime_historically_weak: bool
    slippage_deviation: float
    missed_fill_rate: float
    verdict_reason: str
    recommended_action: str  # continue | reduce | pause | stop
    trade_count: int
    #: Which named signals tripped (from "sharpe", "win_rate", "avg_trade",
    #: "loss_distribution", "slippage") — `promotion_gate`'s execution-quality
    #: check reads this rather than re-deriving a threshold without settings.
    signals_tripped: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    """PRD §9.3's five promotion-gate conditions. ALL required — trade count
    alone has never been sufficient."""

    passed: bool
    trade_count_ok: bool
    deviation_ok: bool
    regime_coverage_ok: bool
    execution_ok: bool
    health_ok: bool
    reasons: tuple[str, ...]  # names of the conditions that failed


def _slice_result(result: BacktestResult, mask: np.ndarray) -> BacktestResult:
    """One era of a `BacktestResult`, with equity recompounded from 1.0 at
    the era's own first bar — what `risk_breach` needs to measure loss
    *since this era began*, not since the full-panel backtest's start."""
    portfolio = result.portfolio_returns[mask]
    return replace(
        result,
        dates=result.dates[mask],
        weights=result.weights[mask],
        gross_returns=result.gross_returns[mask],
        costs=result.costs[mask],
        net_returns=result.net_returns[mask],
        portfolio_returns=portfolio,
        equity=np.cumprod(1.0 + portfolio),
        non_finite_bars=int((~np.isfinite(portfolio)).sum()),
    )


def replay(
    conn: sqlite3.Connection, deployment: Row, *, cost_multiplier: float = DEFAULT_COST_MULTIPLIER
) -> ReplayResult | None:
    """Backtest the deployed spec over the newest usable snapshot and split
    the result at `deployment["started_at"]`.

    Returns `None` when no snapshot has been ingested yet for this
    (market, timeframe, asset_class) — an expected condition before
    `COLLECT_MARKET_DATA` has run, not an error; the caller skips the check
    rather than failing the job.
    """
    strategy = StrategyRepository(conn).get(deployment["strategy_id"])
    if strategy is None:
        raise ValueError(f"no strategy {deployment['strategy_id']}")

    experiment = ExperimentRepository(conn).get(deployment["experiment_id"])
    if experiment is None or experiment["data_snapshot_id"] is None:
        raise ValueError(
            f"deployment {deployment['id']}'s experiment has no data_snapshot_id "
            "to derive an asset_class from"
        )
    origin_snapshot = SnapshotRepository(conn).get(experiment["data_snapshot_id"])
    if origin_snapshot is None:
        raise ValueError(f"no snapshot {experiment['data_snapshot_id']}")
    asset_class = origin_snapshot["asset_class"]

    snapshot = SnapshotRepository(conn).latest_usable(strategy["market"], strategy["timeframe"], asset_class)
    if snapshot is None:
        return None

    resolved = ProfileLoader().resolve(strategy["market"], strategy["timeframe"], asset_class)
    spec = SpecRepository(conn).load_spec(experiment["spec_id"])
    compiled = compile_spec(spec, resolved)

    frame = SnapshotManager(conn).load(snapshot["id"])
    panel = PricePanel.from_frame(frame)

    signals = full_signals(compiled, panel)
    result = run_backtest(panel, signals, resolved, cost_multiplier=cost_multiplier)

    # `label_regimes` runs over the whole panel (length n_bars); `result`'s
    # arrays all drop bar 0 (no prior close to return against) — the same
    # `[1:]` `run_backtest` itself applies, so this stays aligned with
    # `result.dates` rather than introducing a second, independent offset.
    labels = label_regimes(market_returns(panel))[1:]

    started = np.datetime64(deployment["started_at"][:10])
    baseline_mask = result.dates <= started
    paper_mask = ~baseline_mask

    trades = extract_trades(result)
    started_str = str(started)
    baseline_trades = [trade for trade in trades if trade.entry_date <= started_str]
    paper_trades = [trade for trade in trades if trade.entry_date > started_str]

    baseline_result = _slice_result(result, baseline_mask)
    paper_result = _slice_result(result, paper_mask)

    baseline_labels = labels[baseline_mask]
    regime_slices = regime_performance(
        baseline_result.portfolio_returns, baseline_result.dates, baseline_labels, resolved.periods_per_year
    )
    weak_regimes = frozenset(slice_.regime for slice_ in regime_slices if slice_.sharpe < 0.0)

    current_regime = str(labels[-1]) if labels.size else None
    regime_by_date = {str(date): str(label) for date, label in zip(result.dates, labels)}

    return ReplayResult(
        resolved=resolved,
        baseline_trades=baseline_trades,
        paper_trades=paper_trades,
        baseline_result=baseline_result,
        paper_result=paper_result,
        weak_regimes=weak_regimes,
        current_regime=current_regime,
        regime_by_date=regime_by_date,
    )


def risk_breach(equity: np.ndarray, settings: Settings) -> int | None:
    """First bar index where cumulative loss or peak-to-trough drawdown
    since era-start exceeds its hard limit (TRD §18). `None` means no
    breach.

    Trades after this bar must never be recorded — that is what makes a
    100% drawdown structurally unreachable, not merely reported after the
    fact once it has already happened.
    """
    if equity.size == 0:
        return None
    peak = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = np.where(peak > 0.0, 1.0 - equity / peak, 0.0)
    cumulative_loss = 1.0 - equity
    breached = (cumulative_loss > settings.risk_max_loss_pct) | (drawdown > settings.risk_max_drawdown_pct)
    hits = np.flatnonzero(breached)
    return int(hits[0]) if hits.size else None


def _zscore(observed: float, expected: float, se: float) -> float:
    """`(observed - expected) / se`, with an honest answer when `se` itself
    is zero: a real deviation from a genuinely zero-variance baseline is not
    "no evidence" (0.0 would read as perfectly healthy) — it is maximally
    significant, so it saturates at a large magnitude with the deviation's
    sign rather than collapsing to a neutral reading."""
    diff = observed - expected
    if np.isfinite(se) and se > 0.0:
        return diff / se
    return 0.0 if diff == 0.0 else math.copysign(1e6, diff)


def _shape(returns: np.ndarray) -> tuple[float, float]:
    """Skew and (Pearson) kurtosis, with the same near-zero-variance
    fallback `compute_honest_score` uses — a flat or empty series has no
    meaningfully defined higher moments, and scipy returns NaN right where
    the formula needs a number."""
    if returns.size <= 3:
        return 0.0, 3.0
    dispersion = returns.std(ddof=1)
    scale = np.abs(returns).mean()
    if dispersion <= 1e-9 * max(scale, 1.0):
        return 0.0, 3.0
    return (
        float(scipy_stats.skew(returns, bias=False)),
        float(scipy_stats.kurtosis(returns, fisher=False, bias=False)),
    )


def health_verdict(result: ReplayResult, deployment: Row, settings: Settings) -> HealthVerdict:
    """PRD §9.4's Green/Yellow/Orange/Red verdict.

    The regime rule is the point of the stage: a level that would otherwise
    be red or orange is demoted one step when the current regime is one this
    strategy's own baseline shows it historically struggles in — never
    demoted below yellow, since an expected drawdown is still worth flagging,
    just not worth killing over.
    """
    metrics: CoreMetrics = compute_metrics(result.paper_result, result.paper_trades, result.resolved.periods_per_year)
    paper_closed = closed_trades(result.paper_trades)
    baseline_closed = closed_trades(result.baseline_trades)
    n_paper = len(paper_closed)

    if n_paper < settings.health_min_trades_for_verdict:
        return HealthVerdict(
            level="green",
            live_sharpe=metrics.sharpe,
            live_max_dd=metrics.max_drawdown,
            live_win_rate=metrics.win_rate,
            live_profit_factor=metrics.profit_factor,
            sharpe_zscore=None,
            win_rate_zscore=None,
            avg_trade_zscore=None,
            loss_distribution_pvalue=None,
            current_regime=result.current_regime,
            regime_historically_weak=False,
            slippage_deviation=0.0,
            missed_fill_rate=0.0,
            verdict_reason=(
                f"only {n_paper} paper trades — below the "
                f"{settings.health_min_trades_for_verdict}-trade floor for a verdict"
            ),
            recommended_action="continue",
            trade_count=n_paper,
            signals_tripped=(),
        )

    # -- sharpe: SE of the paper era's own Sharpe estimate ---------------------
    paper_returns = result.paper_result.portfolio_returns
    skew, kurtosis = _shape(paper_returns)
    se_sr = se_sharpe(metrics.sharpe, skew, kurtosis, int(paper_returns.size))
    expected_sharpe = deployment["expected_sharpe"] or 0.0
    sharpe_z = _zscore(metrics.sharpe, expected_sharpe, se_sr)

    # -- win rate: binomial SE against the expected (baseline) proportion ------
    expected_win_rate = deployment["expected_win_rate"] or 0.0
    se_wr = math.sqrt(expected_win_rate * (1.0 - expected_win_rate) / n_paper) if 0.0 < expected_win_rate < 1.0 else 0.0
    win_rate_z = _zscore(metrics.win_rate, expected_win_rate, se_wr)

    # -- avg trade: SE of the mean, using the BASELINE era's own dispersion ----
    baseline_returns = np.array([trade.net_return for trade in baseline_closed], dtype=float)
    std_baseline = float(baseline_returns.std(ddof=1)) if baseline_returns.size > 1 else 0.0
    se_avg = std_baseline / math.sqrt(n_paper) if std_baseline > 0.0 else 0.0
    expected_avg_trade = deployment["expected_avg_trade"] or 0.0
    avg_trade_z = _zscore(metrics.avg_trade_return, expected_avg_trade, se_avg)

    # -- loss distribution: baseline losses vs paper losses, KS two-sample -----
    baseline_losses = [trade.net_return for trade in baseline_closed if trade.net_return < 0.0]
    paper_losses = [trade.net_return for trade in paper_closed if trade.net_return < 0.0]
    if len(baseline_losses) >= 2 and len(paper_losses) >= 2:
        ks_result = scipy_stats.ks_2samp(baseline_losses, paper_losses)
        pvalue = float(ks_result.pvalue)  # type: ignore[attr-defined]
    else:
        pvalue = 1.0  # too few loss observations to judge — no evidence against, not evidence for

    # -- execution quality — always exact in a snapshot-replay simulation ------
    # `actual_slippage_bps` is `expected_slippage_bps` by construction (both
    # come from the same `cost_model.slippage_bps`); this dimension only
    # becomes real against a broker (known limit, stated in the plan, not
    # papered over with invented jitter).
    slippage_deviation = 0.0
    missed_fill_rate = 0.0

    tripped: list[str] = []
    if sharpe_z < settings.health_zscore_yellow:
        tripped.append("sharpe")
    if win_rate_z < settings.health_zscore_yellow:
        tripped.append("win_rate")
    if avg_trade_z < settings.health_zscore_yellow:
        tripped.append("avg_trade")
    if pvalue < settings.health_loss_pvalue:
        tripped.append("loss_distribution")
    if slippage_deviation > settings.health_slippage_deviation:
        tripped.append("slippage")

    any_red = (
        sharpe_z < settings.health_zscore_red
        or win_rate_z < settings.health_zscore_red
        or avg_trade_z < settings.health_zscore_red
        or pvalue < settings.health_loss_pvalue
    )
    if any_red:
        level = "red"
    elif len(tripped) >= settings.health_orange_signals:
        level = "orange"
    elif tripped:
        level = "yellow"
    else:
        level = "green"

    regime_weak = result.current_regime is not None and result.current_regime in result.weak_regimes
    demoted = False
    if regime_weak and level == "red":
        level, demoted = "orange", True
    elif regime_weak and level == "orange":
        level, demoted = "yellow", True

    reason = f"tripped: {', '.join(tripped)}" if tripped else "within expectations"
    if demoted:
        reason += f"; demoted one level — {result.current_regime} is a historically weak regime for this strategy"

    return HealthVerdict(
        level=level,
        live_sharpe=metrics.sharpe,
        live_max_dd=metrics.max_drawdown,
        live_win_rate=metrics.win_rate,
        live_profit_factor=metrics.profit_factor,
        sharpe_zscore=sharpe_z,
        win_rate_zscore=win_rate_z,
        avg_trade_zscore=avg_trade_z,
        loss_distribution_pvalue=pvalue,
        current_regime=result.current_regime,
        regime_historically_weak=regime_weak,
        slippage_deviation=slippage_deviation,
        missed_fill_rate=missed_fill_rate,
        verdict_reason=reason,
        recommended_action={"green": "continue", "yellow": "reduce", "orange": "pause", "red": "stop"}[level],
        trade_count=n_paper,
        signals_tripped=tuple(tripped),
    )


def promotion_gate(deployment: Row, verdict: HealthVerdict) -> GateResult:
    """PRD §9.3 — ALL five conditions required. `deviation_ok` (not red) and
    `health_ok` (fully green) are deliberately separate checks even though
    `health_ok` implies `deviation_ok`: PRD §9.3 lists them as two distinct
    bullets, and the AND-gate is unaffected either way."""
    trade_count_ok = deployment["trades_completed"] >= (deployment["trades_required"] or 0)
    deviation_ok = verdict.level != "red"
    required = set(deployment["regimes_required"] or [])
    observed = set(deployment["regimes_observed"] or [])
    regime_coverage_ok = bool(required) and required.issubset(observed)
    execution_ok = "slippage" not in verdict.signals_tripped
    health_ok = verdict.level == "green"

    checks = {
        "trade count": trade_count_ok,
        "deviation from validation": deviation_ok,
        "regime coverage": regime_coverage_ok,
        "execution quality": execution_ok,
        "health checks": health_ok,
    }
    failed = tuple(name for name, ok in checks.items() if not ok)
    return GateResult(
        passed=not failed,
        trade_count_ok=trade_count_ok,
        deviation_ok=deviation_ok,
        regime_coverage_ok=regime_coverage_ok,
        execution_ok=execution_ok,
        health_ok=health_ok,
        reasons=failed,
    )


# -- human-triggered lifecycle actions (App-Flow §11.1, §11.2) -----------------


def kill(conn: sqlite3.Connection, deployment_id: int, *, by: str, reason: str) -> dict[str, Any]:
    """The human-triggerable kill switch (App-Flow §11.1) — the same stop
    `handlers/monitor.py` performs on a risk breach, with `triggered_by` the
    only difference: `'human'` here, `'automatic_rule'` there."""
    if not reason or not reason.strip():
        raise ValueError("a kill switch trip requires a reason")

    deployments = DeploymentRepository(conn)
    deployment = deployments.get(deployment_id)
    if deployment is None:
        raise ValueError(f"no deployment {deployment_id}")
    if deployment["status"] not in ("active", "paused"):
        raise ValueError(f"deployment {deployment_id} is already {deployment['status']}")

    with transaction(conn, immediate=True):
        deployments.update(deployment_id, status="stopped", ended_at=utcnow_iso())
        LifecycleEventRepository(conn).insert(
            deployment_id=deployment_id,
            strategy_id=deployment["strategy_id"],
            event_type="kill_switch",
            from_state=deployment["status"],
            to_state="stopped",
            triggered_by="human",
            reason=f"{by}: {reason}",
        )
    return {"deployment_id": deployment_id, "status": "stopped"}


def retire(conn: sqlite3.Connection, deployment_id: int, *, by: str, reason: str) -> dict[str, Any]:
    """Retirement (App-Flow §11.2): stop the deployment, remove its file
    from the deploy branch — never from `strategy/<id>` — and record it as a
    knowledge entry. *"Edge decay is itself a research finding."*

    Git runs OUTSIDE the transaction, the same `run`/`persist` split
    `gates.approve` uses for its merge; `StrategyRepo.remove_from_branch` is
    idempotent by git's own semantics, so a crash between the removal and
    the database write below is safe to recover by re-running `retire`.
    """
    if not reason or not reason.strip():
        raise ValueError("retirement requires a reason")

    deployments = DeploymentRepository(conn)
    deployment = deployments.get(deployment_id)
    if deployment is None:
        raise ValueError(f"no deployment {deployment_id}")
    if deployment["status"] == "retired":
        raise ValueError(f"deployment {deployment_id} is already retired")

    strategy = StrategyRepository(conn).get(deployment["strategy_id"])
    if strategy is None:
        raise ValueError(f"no strategy {deployment['strategy_id']}")

    from .vcs import StrategyRepo  # deferred: pulls in vcs.py's fcntl dependency

    repo = StrategyRepo(get_settings().strategy_repo_path)
    removal_commit = None
    if deployment["deploy_branch"]:
        code_path = strategy.get("code_path") or code_path_for(strategy["uid"])
        removal_commit = repo.remove_from_branch(
            deployment["deploy_branch"],
            code_path,
            f"retire {strategy['name']} ({deployment['uid']})\n\ndeployment_uid: {deployment['uid']}",
        )

    with transaction(conn, immediate=True):
        deployments.update(deployment_id, status="retired", ended_at=utcnow_iso(), retirement_reason=reason)
        LifecycleEventRepository(conn).insert(
            deployment_id=deployment_id,
            strategy_id=strategy["id"],
            event_type="retired",
            from_state=deployment["status"],
            to_state="retired",
            triggered_by="human",
            reason=reason,
            evidence={"removal_commit": removal_commit},
        )
        transition(conn, "strategies", strategy["id"], "retired", actor=f"human:{by}", reasoning=reason)
        KnowledgeEntryRepository(conn).record(
            entry_type="lesson",
            scope="family",
            strategy_id=strategy["id"],
            title=f"Retired: {strategy['name']}",
            statement=reason,
            evidence={"experiment_ids": [], "failure_reasons": []},
            evidence_count=0,
            applicable_markets=[strategy["market"]],
            applicable_timeframes=[strategy["timeframe"]],
            future_ideas=[f"Why did {strategy['name']} decay, and does the same pattern show up elsewhere?"],
        )

    return {"deployment_id": deployment_id, "strategy_status": "retired", "removal_commit": removal_commit}

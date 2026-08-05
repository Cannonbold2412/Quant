"""Return series, recomputed on demand — for the Decisions review page's
charts and portfolio-correlation panel only.

`evaluations.equity_curve_path` exists in the schema (Backend-Schema §6) but
**no producer ever writes it** — nothing in Stages 0-11 populates it. Rather
than add a second write path to the eval pipeline for the dashboard's sake,
this recomputes through the exact chain already validated:

    compile_spec -> full_signals -> run_backtest

the same three calls `monitoring.replay` (Stage 11) makes over a deployed
spec. `full_signals` was made public in that stage precisely so a second
caller could share it (TRD §6.1's "exactly one implementation" rule).

**This is a full-panel replay, not the concatenated walk-forward series the
honest score was computed from** (TRD §8's best-of-three-windows scheme) —
a real difference, not a rounding one, and every caller must say so in the
page rather than let the chart imply otherwise. `test_mask` below marks
bars that fall inside a stored fold's `[test_start, test_end]` window, which
lets the equity chart shade real out-of-sample evidence even though the
curve itself is drawn from a different (full-panel) run than the fold
metrics were scored on.

One process-lifetime cache, keyed by `(experiment_id, data_snapshot_id)` —
deterministic given both, so a human re-visiting the same review page during
one session pays the backtest once, not per click.
"""
from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np

from ..data.snapshots import SnapshotManager
from ..db.repositories import (
    DeploymentRepository,
    ExperimentRepository,
    SnapshotRepository,
    SpecRepository,
    StrategyRepository,
)
from ..eval.backtest import BacktestResult, run_backtest
from ..eval.engine import full_signals
from ..eval.panel import PricePanel
from ..operators.compile import compile_spec
from ..profiles import ProfileLoader

__all__ = ["backtest_for_experiment", "portfolio_correlation", "test_window_mask"]

#: The same "realistic, non-stress" default `monitoring.DEFAULT_COST_MULTIPLIER`
#: uses — neither `experiments` nor `evaluations` persists the multiplier a
#: given result was scored at, so a recomputation has no per-experiment
#: provenance to recover and falls back to the system-wide default, exactly
#: as `monitoring.replay` already does.
DEFAULT_COST_MULTIPLIER = 2.0

_CACHE: dict[tuple[int, int], BacktestResult] = {}


def backtest_for_experiment(conn: sqlite3.Connection, experiment_id: int) -> BacktestResult | None:
    """Replay one experiment's winning spec over the snapshot it was
    actually evaluated against. `None` when the experiment, its spec or its
    snapshot cannot be resolved — the caller renders an empty chart rather
    than raising, since a promotion candidate missing this is a data gap to
    show, not a request to fail the whole page."""
    experiment = ExperimentRepository(conn).get(experiment_id)
    if experiment is None or experiment.get("spec_id") is None or experiment.get("data_snapshot_id") is None:
        return None

    snapshot_id = experiment["data_snapshot_id"]
    key = (experiment_id, snapshot_id)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    strategy = StrategyRepository(conn).get(experiment["strategy_id"])
    snapshot = SnapshotRepository(conn).get(snapshot_id)
    if strategy is None or snapshot is None:
        return None

    resolved = ProfileLoader().resolve(strategy["market"], strategy["timeframe"], snapshot["asset_class"])
    spec = SpecRepository(conn).load_spec(experiment["spec_id"])
    compiled = compile_spec(spec, resolved)

    frame = SnapshotManager(conn).load(snapshot_id)
    panel = PricePanel.from_frame(frame)
    signals = full_signals(compiled, panel)
    result = run_backtest(panel, signals, resolved, cost_multiplier=DEFAULT_COST_MULTIPLIER)

    _CACHE[key] = result
    return result


def test_window_mask(dates: np.ndarray, fold_metrics: list[dict[str, Any]] | None) -> np.ndarray:
    """One bool per bar in `dates`: True where that date falls inside any
    stored fold's `[test_start, test_end]` (`evaluations.fold_metrics`,
    `WalkForwardFold.metrics` — Stage 3's own per-fold record). Used to
    shade the equity chart's out-of-sample windows; see this module's
    docstring for why that is a real-but-imperfect signal against a
    full-panel replay."""
    mask = np.zeros(len(dates), dtype=bool)
    if not fold_metrics:
        return mask
    date_strs = np.array([str(d)[:10] for d in dates])
    for fold in fold_metrics:
        start, end = fold.get("test_start"), fold.get("test_end")
        if not start or not end:
            continue
        mask |= (date_strs >= start) & (date_strs <= end)
    return mask


def portfolio_correlation(conn: sqlite3.Connection, experiment_id: int) -> list[dict[str, Any]]:
    """Correlation between a candidate's replayed daily returns and every
    currently-active deployment's own — App-Flow §7.2 / UI-UX-Brief §3.2:
    "computed fresh by plain Python from stored return series", shown to the
    human, **never** written back into `promotions` and never fed to A4.
    Deployments with too few overlapping bars (<10) are skipped rather than
    reported at a meaningless correlation."""
    candidate = backtest_for_experiment(conn, experiment_id)
    if candidate is None:
        return []
    candidate_series = dict(zip((str(d)[:10] for d in candidate.dates), candidate.portfolio_returns))

    out: list[dict[str, Any]] = []
    for deployment in DeploymentRepository(conn).active():
        other_experiment_id = deployment.get("experiment_id")
        if other_experiment_id is None or other_experiment_id == experiment_id:
            continue
        other = backtest_for_experiment(conn, other_experiment_id)
        if other is None:
            continue
        other_series = dict(zip((str(d)[:10] for d in other.dates), other.portfolio_returns))

        common = sorted(set(candidate_series) & set(other_series))
        if len(common) < 10:
            continue
        a = np.array([candidate_series[d] for d in common])
        b = np.array([other_series[d] for d in common])
        correlation = 0.0 if a.std() == 0.0 or b.std() == 0.0 else float(np.corrcoef(a, b)[0, 1])

        strategy = StrategyRepository(conn).get(deployment["strategy_id"])
        out.append(
            {
                "deployment_uid": deployment["uid"],
                "strategy_name": strategy.get("name") if strategy else None,
                "mode": deployment.get("mode"),
                "correlation": correlation,
                "n_common_bars": len(common),
            }
        )
    return sorted(out, key=lambda row: abs(row["correlation"]), reverse=True)

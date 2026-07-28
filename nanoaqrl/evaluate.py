"""evaluate.py — the scoring harness and the hard bar.

Agent permission (TRD §2.1, §2.3): **neither readable nor writable.** A
backtest score does not survive being understood — an agent that can read
this file will eventually exploit a weakness in it, not from malice but
because exploiting the measurement is the cheapest path to a higher number
(TRD §15.1). Full OS-level enforcement (a separate Unix user, `chmod 700`) is
listed as an open question in Implementation_Plan §21 and is not solved
here — this file only implements the scoring logic itself and the one
structural isolation that *is* implemented: it always scores the immutable
committed source of `strategy.py`, read via `git show`, never the possibly-
since-edited working tree.

Usage:
    python -m nanoaqrl.evaluate score <commit> [--family F] [--name N] [--description D]
    python -m nanoaqrl.evaluate null-world --generator {permuted,block_bootstrap,synthetic_path} --replications N
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import types
from pathlib import Path

import numpy as np

from . import data
from ._lib import db
from ._lib.backtest import empirical_leakage_scan, max_drawdown_from_returns, static_lookahead_scan
from ._lib.synthetic_data import block_bootstrap_ohlcv, permuted_returns_ohlcv, synthetic_path_ohlcv
from ._lib.walk_forward import run_best_of_three

EVAL_ENGINE_VERSION = "nanoaqrl-0.1.0"

# ---------------------------------------------------------------------------
# The pre-registered bar (TRD §7.5). Enforced HERE, not merely stated in
# program.md — program.md states the target, this file enforces it.
# ---------------------------------------------------------------------------
MIN_HONEST_SCORE = 0.50
MAX_OOS_DRAWDOWN = {"default": 0.15, "crypto": 0.20}
MIN_TRADES = 100
Z_MULTIPLIER = 1.65

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_TSV = Path(__file__).resolve().parent / "results.tsv"
DB_PATH = Path(__file__).resolve().parent / "nanoaqrl.db"


def _wf_config_hash(test_years: int, train_years: tuple[int, ...]) -> str:
    payload = f"scheme=rolling;test_years={test_years};train_years={sorted(train_years)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_strategy_source(commit: str) -> str:
    """Read strategy.py from an immutable git commit — never the working
    tree, so a later edit can never retroactively change what was scored."""
    result = subprocess.run(
        ["git", "show", f"{commit}:nanoaqrl/strategy.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def exec_strategy_module(source: str) -> types.SimpleNamespace:
    """Executes the committed source in a fresh namespace. Note: this is
    *not* a security sandbox — no network/credential isolation is applied.
    Real agent sandboxing (no network, no credentials, subprocess isolation)
    is Stage 5's job (TRD §18); nanoAQRL's guarantee is narrower: the source
    scored is always the immutable commit, never the live working tree."""
    namespace: dict = {"__name__": "strategy"}
    exec(compile(source, "strategy.py", "exec"), namespace)
    return types.SimpleNamespace(
        generate_signals=namespace["generate_signals"],
        params=namespace["PARAMS"],
        param_grid=namespace.get("PARAM_GRID"),
    )


def estimate_holding_period_days(df, generate_signals, params, cost_model) -> int:
    """A coarse P1-style pre-check: one full-sample backtest just to gauge
    roughly how long positions are held, so the walk-forward embargo can be
    set >= holding period (TRD §8.4)."""
    from ._lib.backtest import run_backtest

    result = run_backtest(df, generate_signals, params, cost_model, cost_multiplier=1.0)
    if result.n_trades == 0:
        return 5
    est = len(df) / result.n_trades
    return int(np.clip(est, 1, 60))


def _bar_check(honest_score: float, n_trades: int, max_dd: float, oos_returns: np.ndarray, is_crypto: bool = False):
    """Returns (passed: bool, failed_on: str | None). Checked in this order;
    the first failure short-circuits the rest (TRD §7.5)."""
    if n_trades < MIN_TRADES:
        return False, "min_trades"
    dd_limit = MAX_OOS_DRAWDOWN["crypto" if is_crypto else "default"]
    if max_dd > dd_limit:
        return False, "max_drawdown"
    if oos_returns.sum() <= 0:  # already computed at 2x costs throughout (TRD §7.2)
        return False, "cost_stress"
    if honest_score < MIN_HONEST_SCORE:
        return False, "min_score"
    return True, None


def score_commit(commit: str, family: str, name: str, description: str, conn=None) -> dict:
    conn = conn or db.get_connection(DB_PATH)
    strategy_id = db.get_or_create_strategy(conn, name=name, family=family, market=data.MARKET, timeframe=data.TIMEFRAME)
    iteration = db.next_iteration(conn, strategy_id)
    wf_hash = _wf_config_hash(test_years=1, train_years=(1, 2, 3))
    experiment_id = db.insert_experiment(
        conn, strategy_id, iteration, code_commit=commit, wf_config_hash=wf_hash,
        eval_engine_version=EVAL_ENGINE_VERSION, random_seed=iteration,
    )

    try:
        source = load_strategy_source(commit)
    except subprocess.CalledProcessError as e:
        db.complete_experiment(conn, experiment_id, "crash", "bar", "error", "commit_not_found")
        db.append_results_tsv(RESULTS_TSV, commit, None, 0, "crash", f"could not read strategy.py@{commit}: {e}")
        return {"status": "crash", "reason": "commit_not_found"}

    static_violations = static_lookahead_scan(source)
    if static_violations:
        db.complete_experiment(conn, experiment_id, "discard", "P0", "failed", "lookahead_static")
        db.insert_evaluation(conn, experiment_id, "P0", "fail")
        db.append_results_tsv(RESULTS_TSV, commit, None, 0, "discard", "P0 static: " + "; ".join(static_violations))
        return {"status": "discard", "phase": "P0", "violations": static_violations}

    module = exec_strategy_module(source)
    start, end = data.research_range()
    df = data.get_ohlcv(data.INSTRUMENT, start, end)

    empirical_violations = empirical_leakage_scan(df, module.generate_signals, module.params)
    if empirical_violations:
        db.complete_experiment(conn, experiment_id, "discard", "P0", "failed", "lookahead_empirical")
        db.insert_evaluation(conn, experiment_id, "P0", "fail")
        db.append_results_tsv(RESULTS_TSV, commit, None, 0, "discard", "P0 empirical: " + "; ".join(empirical_violations))
        return {"status": "discard", "phase": "P0", "violations": empirical_violations}

    cost_model = data.cost_model()
    holding_period = estimate_holding_period_days(df, module.generate_signals, module.params, cost_model)
    n_trials_base = db.get_family_trial_count(conn, family) + 1  # this attempt counts too

    result = run_best_of_three(
        df, module.generate_signals, module.params, cost_model,
        holding_period_days=holding_period, n_trials_base=n_trials_base,
        param_grid=module.param_grid, test_years=1, cost_multiplier=2.0,
        periods_per_year=data.PERIODS_PER_YEAR, z_multiplier=Z_MULTIPLIER,
    )
    win = result.winning_window
    max_dd = max_drawdown_from_returns(win.concatenated_returns)
    passed, failed_on = _bar_check(win.score.honest_score, win.total_trades, max_dd, win.concatenated_returns)

    db.insert_evaluation(
        conn, experiment_id, "P3", "pass" if passed else "fail",
        bar_result="pass" if passed else "fail", bar_failed_on=failed_on,
        honest_score=win.score.honest_score, sr_oos=win.score.sr_oos, se_sr=win.score.se_sr,
        z_multiplier=Z_MULTIPLIER, trials_haircut=win.score.trials_haircut, n_trials=win.score.n_trials,
        winning_train_years=result.winning_train_years,
        score_1yr=result.windows[1].score.honest_score,
        score_2yr=result.windows[2].score.honest_score,
        score_3yr=result.windows[3].score.honest_score,
        n_trades=win.total_trades, max_drawdown=max_dd,
    )

    plateau = db.record_plateau_step(conn, strategy_id, cleared=passed)

    if passed:
        db.complete_experiment(conn, experiment_id, "keep", "P3", "passed")
        db.record_bar_clear(conn, strategy_id, experiment_id, win.score.honest_score)
        db.append_results_tsv(
            RESULTS_TSV, commit, win.score.honest_score, win.total_trades, "keep",
            f"{description} | bar cleared, train_years={result.winning_train_years} | STOP",
        )
        return {"status": "keep", "score": win.score.honest_score, "winning_train_years": result.winning_train_years}
    else:
        db.complete_experiment(conn, experiment_id, "discard", "P3", "failed", failed_on)
        db.append_results_tsv(
            RESULTS_TSV, commit, None, win.total_trades, "discard",
            f"{description} | bar failed on {failed_on} | honest_score={win.score.honest_score:.4f} | plateau={plateau}",
        )
        return {"status": "discard", "failed_on": failed_on, "honest_score": win.score.honest_score, "plateau": plateau}


def run_null_world(
    generator: str,
    n_replications: int,
    n_days: int = 252 * 6,
    seed_base: int = 1000,
    module: types.SimpleNamespace | None = None,
) -> dict:
    """TRD §15.3, Milestone 0: run the identical scoring path against data
    with no alpha by construction, and count how many replications 'discover'
    something. That count is the measured false discovery rate.

    `module` lets callers (tests) inject a strategy directly; the CLI path
    defaults to reading the committed `strategy.py` from HEAD."""
    generators = {
        "permuted": permuted_returns_ohlcv,
        "block_bootstrap": block_bootstrap_ohlcv,
        "synthetic_path": synthetic_path_ohlcv,
    }
    if generator not in generators:
        raise ValueError(f"unknown generator: {generator}")
    gen_fn = generators[generator]

    module = module or exec_strategy_module(load_strategy_source("HEAD"))
    cost_model = data.cost_model()
    n_discoveries = 0
    max_score = -np.inf

    for i in range(n_replications):
        df = gen_fn(n_days, seed=seed_base + i)
        holding_period = estimate_holding_period_days(df, module.generate_signals, module.params, cost_model)
        result = run_best_of_three(
            df, module.generate_signals, module.params, cost_model,
            holding_period_days=holding_period, n_trials_base=1,
            param_grid=module.param_grid, test_years=1, cost_multiplier=2.0,
            periods_per_year=data.PERIODS_PER_YEAR, z_multiplier=Z_MULTIPLIER,
        )
        win = result.winning_window
        if win.concatenated_returns.size == 0:
            continue
        max_dd = max_drawdown_from_returns(win.concatenated_returns)
        passed, _ = _bar_check(win.score.honest_score, win.total_trades, max_dd, win.concatenated_returns)
        max_score = max(max_score, win.score.honest_score)
        if passed:
            n_discoveries += 1

    conn = db.get_connection(DB_PATH)
    db.insert_null_world_run(conn, generator, n_replications, n_discoveries, float(max_score), EVAL_ENGINE_VERSION)
    return {
        "generator": generator,
        "n_replications": n_replications,
        "n_discoveries": n_discoveries,
        "fdr": n_discoveries / n_replications,
        "max_score_observed": float(max_score),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="nanoAQRL evaluate.py")
    sub = parser.add_subparsers(dest="command", required=True)

    score_p = sub.add_parser("score")
    score_p.add_argument("commit")
    score_p.add_argument("--family", default="ma_crossover_trend")
    score_p.add_argument("--name", default="ma_crossover_trend_v1")
    score_p.add_argument("--description", default="dual MA crossover")

    null_p = sub.add_parser("null-world")
    null_p.add_argument("--generator", choices=["permuted", "block_bootstrap", "synthetic_path"], required=True)
    null_p.add_argument("--replications", type=int, default=30)
    null_p.add_argument("--days", type=int, default=252 * 6)

    args = parser.parse_args()

    if args.command == "score":
        outcome = score_commit(args.commit, args.family, args.name, args.description)
    else:
        outcome = run_null_world(args.generator, args.replications, args.days)

    for k, v in outcome.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()

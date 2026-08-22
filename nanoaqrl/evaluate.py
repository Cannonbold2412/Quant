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
import types
from pathlib import Path

import numpy as np

from aqrl.eval.metrics import drawdown_series, longest_drawdown_days
from aqrl.eval.stats.monte_carlo import monte_carlo_paths

from . import data
from ._lib import db
from ._lib.backtest import empirical_leakage_scan, max_drawdown_from_returns, static_lookahead_scan
from ._lib.synthetic_data import block_bootstrap_ohlcv, permuted_returns_ohlcv, synthetic_path_ohlcv
from ._lib.walk_forward import run_best_of_three

EVAL_ENGINE_VERSION = "nanoaqrl-0.1.0"

# ---------------------------------------------------------------------------
# The pre-registered bar (TRD §7.5). Enforced HERE, not merely stated in
# program.md — program.md states the target, this file enforces it.
#
# The first three are **validity floors**: a strategy that trades 4 times, or
# gives back a fifth of its capital, or is only profitable before costs, has
# not produced a result worth ranking at any score. They never move.
#
# MIN_HONEST_SCORE is the *entry* threshold only. Once a strategy has cleared
# it once, `strategies.best_score` takes over as the threshold and every later
# experiment must beat the best score so far — see `_bar_check`. This is a
# deliberate change from "clear the fixed bar once and stop": it keeps the loop
# hill-climbing, at the cost that the reported best is now the maximum over a
# growing number of attempts. `n_trials`/`trials_haircut` in the honest score
# price some of that selection in, but they count grid combinations within one
# experiment, not the experiment count — read `results.tsv` accordingly.
# ---------------------------------------------------------------------------
MIN_HONEST_SCORE = 0.50
MAX_OOS_DRAWDOWN = {"default": 0.15, "crypto": 0.20}
MIN_TRADES = 100
Z_MULTIPLIER = 1.65

#: Block length for the diagnostic bootstrap, in bars. Matched to the reference
#: harness's 20 trading days.
MC_MEAN_BLOCK = 20.0
MC_REPLICATIONS = 500

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_TSV = Path(__file__).resolve().parent / "results.tsv"
DB_PATH = Path(__file__).resolve().parent / "nanoaqrl.db"

# ---------------------------------------------------------------------------
# `results.tsv` schema. Everything past `description` is **diagnostic only** —
# none of it gates a keep. It is here so that a passing score can be
# sanity-checked after the fact instead of taken at face value: a Sharpe that
# rests on three bars, or on one fold out of six, looks identical to a real one
# in a single number.
# ---------------------------------------------------------------------------
RESULTS_COLUMNS = [
    "commit", "status", "score", "n_trades", "description",
    # verdict detail
    "bar_failed_on", "baseline_score", "delta_score", "plateau", "winning_train_years",
    # headline performance
    "sharpe", "cagr_pct", "total_return_pct", "maxdd_pct", "avg_dd_pct",
    "dd_duration_days", "profit_factor",
    # honest-score internals
    "sr_oos", "se_sr", "trials_haircut", "n_trials", "wf_efficiency",
    # best-of-three spread — a score that only exists at one train length is
    # a choice of window, not an edge
    "score_1yr", "score_2yr", "score_3yr",
    # walk-forward stability
    "n_folds", "fold_sharpe_mean", "fold_sharpe_std", "fold_sharpe_min",
    "folds_positive", "folds_gt1", "fold_sharpes",
    "decay_pct", "early_sharpe", "late_sharpe",
    # outlier dependence and tail
    "worst_bar_pct", "best_bar_pct", "top5_bars_pct", "top5pct_bars_pct",
    "worst_bar", "best_bar", "top_fold_pct",
    # monte carlo
    "mc_p5_return", "mc_p50_return", "mc_p95_return", "mc_ruin_prob",
]

#: Columns owned by the verdict, not by `_diagnostics` — kept out of the
#: diagnostics spread so the two can never fight over the same key.
_VERDICT_FIELDS = frozenset({"score", "bar_failed_on", "baseline_score", "delta_score", "plateau"})


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


def _bar_check(
    honest_score: float,
    n_trades: int,
    max_dd: float,
    oos_returns: np.ndarray,
    threshold: float = MIN_HONEST_SCORE,
    ratcheted: bool = False,
    is_crypto: bool = False,
):
    """Returns (passed: bool, failed_on: str | None). Checked in this order;
    the first failure short-circuits the rest (TRD §7.5).

    `threshold` is the pre-registered `MIN_HONEST_SCORE` until this strategy has
    cleared it once, and its own best score afterwards. `ratcheted` says which,
    and changes two things: ties no longer clear (a repeat of the best score is
    not an improvement), and the failure is labelled `baseline` rather than
    `min_score` so the two are distinguishable in `results.tsv`.
    """
    if n_trades < MIN_TRADES:
        return False, "min_trades"
    dd_limit = MAX_OOS_DRAWDOWN["crypto" if is_crypto else "default"]
    if max_dd > dd_limit:
        return False, "max_drawdown"
    if oos_returns.sum() <= 0:  # already computed at 2x costs throughout (TRD §7.2)
        return False, "cost_stress"
    cleared = honest_score > threshold if ratcheted else honest_score >= threshold
    if not cleared:
        return False, "baseline" if ratcheted else "min_score"
    return True, None


def _diagnostics(result, win, periods_per_year: float, seed: int = 0) -> dict:
    """Everything in `results.tsv` past `description`, as a flat dict.

    None of it gates. It exists to answer "would I believe this number if I saw
    it in a table" — how the score is spread across folds, how much of the
    return came from a handful of bars, and what the same edge could plausibly
    have produced in a different order.

    Two deliberate departures from the reference harness this mirrors:

    * **Bars, not trades.** The reference reports per-trade outlier
      concentration; nanoAQRL's single-series engine produces a return per bar
      and no tradebook, so the same question is asked of bars. For a strategy
      holding several days this is a *weaker* concentration signal than the
      trade-level version — a single trade's contribution is spread across its
      bars — so read `top5_bars_pct` as a floor on concentration, not a
      measure of it.
    * **Monte Carlo via `aqrl.eval.stats.monte_carlo`**, which reports the
      distribution of total *return* rather than of Sharpe, and a ruin
      probability. Reusing it rather than writing a second block bootstrap is
      the point: two implementations of one statistical procedure diverge
      silently (TRD §6.1).
    """
    returns = np.asarray(win.concatenated_returns, dtype=float)
    d: dict = dict.fromkeys(
        (column for column in RESULTS_COLUMNS if column not in ("commit", "status", "description")), 0.0
    )
    d["fold_sharpes"] = ""
    d["winning_train_years"] = result.winning_train_years
    d["sr_oos"] = win.score.sr_oos
    d["se_sr"] = win.score.se_sr
    d["trials_haircut"] = win.score.trials_haircut
    d["n_trials"] = win.score.n_trials
    d["wf_efficiency"] = win.wf_efficiency
    d["sharpe"] = win.score.sr_oos
    d["n_trades"] = win.total_trades
    for years in (1, 2, 3):
        window = result.windows.get(years)
        d[f"score_{years}yr"] = window.score.honest_score if window else 0.0

    # ---- walk-forward stability: one number per fold of the winning window --
    folds = list(win.folds)
    fold_sharpes = [float(fold.out_of_sample_sharpe) for fold in folds]
    d["n_folds"] = len(folds)
    if fold_sharpes:
        d["fold_sharpe_mean"] = float(np.mean(fold_sharpes))
        d["fold_sharpe_std"] = float(np.std(fold_sharpes))
        d["fold_sharpe_min"] = float(min(fold_sharpes))
        d["folds_positive"] = sum(s > 0 for s in fold_sharpes)
        d["folds_gt1"] = sum(s > 1 for s in fold_sharpes)
        d["fold_sharpes"] = ",".join(
            f"{fold.window.test_start.year}:{sharpe:.2f}" for fold, sharpe in zip(folds, fold_sharpes)
        )
        # decay and the early/late split both read the same fold series, cut
        # differently. Both need enough folds for the halves to be disjoint;
        # below that they are 0.0, meaning "not enough folds", not "no decay".
        if len(fold_sharpes) > 3:
            first, last = float(np.mean(fold_sharpes[:3])), float(np.mean(fold_sharpes[-3:]))
            d["decay_pct"] = float((first - last) / first * 100.0) if first else 0.0
            d["early_sharpe"] = float(np.mean(fold_sharpes[:-3]))
            d["late_sharpe"] = last

    if returns.size == 0:
        return d

    # ---- headline performance on the concatenated out-of-sample series ------
    total_return = float(np.prod(1.0 + returns) - 1.0)
    # Elapsed time from the bar count and the profile's annualisation, not the
    # calendar span: the concatenated series has embargo gaps cut out of it,
    # and calendar span would count time the strategy was not invested.
    years = returns.size / periods_per_year if periods_per_year else 0.0
    d["total_return_pct"] = total_return * 100.0
    if years > 0 and total_return > -1:
        d["cagr_pct"] = float((1.0 + total_return) ** (1.0 / years) - 1.0) * 100.0
    d["maxdd_pct"] = max_drawdown_from_returns(returns) * 100.0
    underwater = drawdown_series(returns)
    underwater = underwater[underwater < 0.0]
    d["avg_dd_pct"] = float(-underwater.mean()) * 100.0 if underwater.size else 0.0
    d["dd_duration_days"] = longest_drawdown_days(returns, np.asarray(win.concatenated_dates))
    gains, losses = returns[returns > 0].sum(), -returns[returns < 0].sum()
    d["profit_factor"] = float(gains / losses) if losses > 1e-12 else 0.0

    # ---- outlier dependence: how much of the total rests on how little ------
    total = returns.sum()
    d["worst_bar"], d["best_bar"] = float(returns.min()), float(returns.max())
    if abs(total) > 1e-12:
        ranked = np.sort(returns)[::-1]
        top_n = max(1, returns.size // 20)
        d["worst_bar_pct"] = float(returns.min() / total * 100.0)
        d["best_bar_pct"] = float(returns.max() / total * 100.0)
        d["top5_bars_pct"] = float(ranked[:5].sum() / total * 100.0)
        d["top5pct_bars_pct"] = float(ranked[:top_n].sum() / total * 100.0)
        if folds:
            d["top_fold_pct"] = float(max(fold.returns.sum() for fold in folds) / total * 100.0)

    # ---- monte carlo -------------------------------------------------------
    mc = monte_carlo_paths(
        returns, replications=MC_REPLICATIONS, base_seed=seed, mean_block=MC_MEAN_BLOCK
    )
    d["mc_p5_return"] = mc.p5_return
    d["mc_p50_return"] = mc.p50_return
    d["mc_p95_return"] = mc.p95_return
    d["mc_ruin_prob"] = mc.ruin_probability
    return d


def _print_diagnostics(d: dict) -> None:
    print("\n=== diagnostics (not gated) ===")
    print("-- headline --")
    print(f"  sharpe={d['sharpe']:.2f}  cagr={d['cagr_pct']:.1f}%  total_return={d['total_return_pct']:.1f}%")
    print(f"  maxdd={d['maxdd_pct']:.1f}%  avg_dd={d['avg_dd_pct']:.1f}%  "
          f"longest_dd={d['dd_duration_days']:.0f}d  profit_factor={d['profit_factor']:.2f}")
    print(f"  sr_oos={d['sr_oos']:.2f}  se_sr={d['se_sr']:.3f}  haircut={d['trials_haircut']:.3f}  "
          f"n_trials={d['n_trials']}  wf_efficiency={d['wf_efficiency']:.2f}")
    print(f"  best-of-three: 1yr={d['score_1yr']:.3f}  2yr={d['score_2yr']:.3f}  3yr={d['score_3yr']:.3f}  "
          f"(won: {d['winning_train_years']}yr)")

    print("\n-- walk-forward stability --")
    if not d["n_folds"]:
        print("  no folds — nothing to check")
        return
    print(f"  per fold: {d['fold_sharpes']}")
    print(f"  mean={d['fold_sharpe_mean']:.2f} std={d['fold_sharpe_std']:.2f} min={d['fold_sharpe_min']:.2f} "
          f"folds>0={d['folds_positive']}/{d['n_folds']} folds>1={d['folds_gt1']}/{d['n_folds']}")
    if d["n_folds"] > 3:
        print(f"  decay(first3 vs last3)={d['decay_pct']:.0f}%  "
              f"early={d['early_sharpe']:.2f}  late={d['late_sharpe']:.2f}")
    else:
        print(f"  decay/early/late need >3 folds ({d['n_folds']} here) — not computed")

    print("\n-- outlier dependence (per bar; see _diagnostics for why not per trade) --")
    print(f"  best single bar={d['best_bar_pct']:.1f}% of total  worst={d['worst_bar_pct']:.1f}%")
    print(f"  top-5 bars={d['top5_bars_pct']:.1f}%  top-5%-of-bars={d['top5pct_bars_pct']:.1f}%")
    print(f"  best single fold={d['top_fold_pct']:.1f}% of total")
    print(f"  worst bar return={d['worst_bar']:+.4f}  best bar return={d['best_bar']:+.4f}")

    print(f"\n-- monte carlo (stationary bootstrap, mean block={MC_MEAN_BLOCK:.0f} bars, "
          f"n={MC_REPLICATIONS}) --")
    print(f"  total return  p5={d['mc_p5_return']:+.1%}  p50={d['mc_p50_return']:+.1%}  "
          f"p95={d['mc_p95_return']:+.1%}")
    print(f"  P(drawdown >= 50%)={d['mc_ruin_prob']:.1%}")


def _row(commit: str, status: str, description: str, **fields) -> dict:
    """One `results.tsv` row. Floats are formatted here rather than by the
    writer so each column keeps a sensible precision — a raw repr of a numpy
    float is unreadable in a terminal, which is where this file is read."""
    row = {"commit": commit, "status": status, "description": description}
    for key, value in fields.items():
        if value is None:
            row[key] = ""
        elif isinstance(value, float):
            row[key] = f"{value:.6f}" if key in ("score", "baseline_score", "delta_score") else f"{value:.4f}"
        else:
            row[key] = value
    return row


def score_commit(commit: str, family: str, name: str, description: str, conn=None) -> dict:
    conn = conn or db.get_connection(DB_PATH)
    strategy_id = db.get_or_create_strategy(conn, name=name, family=family, market=data.MARKET, timeframe=data.TIMEFRAME)
    iteration = db.next_iteration(conn, strategy_id)
    wf_hash = _wf_config_hash(test_years=1, train_years=(1, 2, 3))
    experiment_id = db.insert_experiment(
        conn, strategy_id, iteration, code_commit=commit, wf_config_hash=wf_hash,
        eval_engine_version=EVAL_ENGINE_VERSION, random_seed=iteration,
        # Stage 1 makes the profile hashes real, so the provenance stamp
        # TRD §6.6 requires is now complete rather than partial.
        market_profile_hash=data.MARKET_PROFILE_HASH,
        timeframe_profile_hash=data.TIMEFRAME_PROFILE_HASH,
        cost_model_hash=data.COST_MODEL_HASH,
    )

    try:
        source = load_strategy_source(commit)
    except subprocess.CalledProcessError as e:
        db.complete_experiment(conn, experiment_id, "crash", "bar", "error", "commit_not_found")
        db.append_results_tsv(
            RESULTS_TSV,
            _row(commit, "crash", f"{description} | could not read strategy.py@{commit}: {e}"),
            RESULTS_COLUMNS,
        )
        return {"status": "crash", "reason": "commit_not_found"}

    static_violations = static_lookahead_scan(source)
    if static_violations:
        db.complete_experiment(conn, experiment_id, "discard", "P0", "failed", "lookahead_static")
        db.insert_evaluation(conn, experiment_id, "P0", "fail")
        db.append_results_tsv(
            RESULTS_TSV,
            _row(commit, "discard", f"{description} | P0 static: " + "; ".join(static_violations),
                 bar_failed_on="lookahead_static"),
            RESULTS_COLUMNS,
        )
        return {"status": "discard", "phase": "P0", "violations": static_violations}

    module = exec_strategy_module(source)
    start, end = data.research_range()
    df = data.get_ohlcv(data.INSTRUMENT, start, end)

    empirical_violations = empirical_leakage_scan(df, module.generate_signals, module.params)
    if empirical_violations:
        db.complete_experiment(conn, experiment_id, "discard", "P0", "failed", "lookahead_empirical")
        db.insert_evaluation(conn, experiment_id, "P0", "fail")
        db.append_results_tsv(
            RESULTS_TSV,
            _row(commit, "discard", f"{description} | P0 empirical: " + "; ".join(empirical_violations),
                 bar_failed_on="lookahead_empirical"),
            RESULTS_COLUMNS,
        )
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

    # The ratchet. `best_score` is None until this strategy has cleared the
    # pre-registered floor once; from then on it is the threshold, and the
    # floor is never consulted again.
    baseline = db.best_score(conn, strategy_id)
    threshold = MIN_HONEST_SCORE if baseline is None else baseline
    passed, failed_on = _bar_check(
        win.score.honest_score, win.total_trades, max_dd, win.concatenated_returns,
        threshold=threshold, ratcheted=baseline is not None,
    )

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
    diag = _diagnostics(result, win, data.PERIODS_PER_YEAR, seed=iteration)
    _print_diagnostics(diag)

    status = "keep" if passed else "discard"
    note = (
        f"beat baseline, train_years={result.winning_train_years}"
        if passed
        else f"failed on {failed_on}, plateau={plateau}"
    )
    row = _row(
        commit, status, f"{description} | {note}",
        score=win.score.honest_score,
        bar_failed_on=failed_on or "",
        baseline_score=baseline,
        delta_score=None if baseline is None else win.score.honest_score - baseline,
        plateau=plateau,
        # `diag` is seeded from RESULTS_COLUMNS, so it carries a zero for every
        # verdict field too. The explicit kwargs above own those.
        **{key: value for key, value in diag.items() if key not in _VERDICT_FIELDS},
    )
    db.append_results_tsv(RESULTS_TSV, row, RESULTS_COLUMNS)

    if passed:
        db.complete_experiment(conn, experiment_id, "keep", "P3", "passed")
        db.record_improvement(conn, strategy_id, experiment_id, win.score.honest_score)
        return {"status": "keep", "score": win.score.honest_score,
                "winning_train_years": result.winning_train_years, "baseline": baseline}
    db.complete_experiment(conn, experiment_id, "discard", "P3", "failed", failed_on)
    return {"status": "discard", "failed_on": failed_on, "honest_score": win.score.honest_score,
            "plateau": plateau, "baseline": baseline}


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

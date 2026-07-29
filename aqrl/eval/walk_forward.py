"""The walk-forward protocol — TRD §8. One implementation, two callers.

Rolling windows, test always 1 year, train evaluated at all three lengths
(1/2/3 yr) with the best reported, purged with an embargo, folds **concatenated**
before scoring. Parameter tuning re-runs from scratch inside each fold, on that
fold's training window only.

**What is injected and what is fixed.** The protocol — fold geometry, the
embargo, tuning on training data, chronological concatenation, best-of-three —
is fixed and lives here. What varies is only *how a date range is turned into a
return series*, which arrives as an `evaluate(start, end, params)` callable. That
is what lets Stage 0's single pandas series and Stage 3's instrument panel share
one protocol instead of two that drift.

**The scheme is not searched over.** Running rolling, then anchored, and
reporting whichever scored better would be a selection *outside* the trial
count, invisible to the haircut. One scheme per campaign, hashed into
`wf_config_hash` (TRD §8.2).

**An emergent property worth naming:** because the agent cannot read or edit
this code, the walk-forward configuration cannot become an iteration lever. A3
may propose changes to the strategy, but never "try a different training
window" — that is part of the evaluation contract, structurally out of reach.
"""
from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

import numpy as np

from .determinism import derive_seed, rng_for
from .parallel import map_ordered
from .stats.honest_score import HonestScoreResult, compute_honest_score, sharpe_ratio

__all__ = [
    "BestOfThreeResult",
    "FoldResult",
    "FoldWindow",
    "SliceOutcome",
    "WindowResult",
    "generate_rolling_folds",
    "run_best_of_three",
    "run_window",
]

#: TRD §8.6: *"recommended starting grid: ≤ 50 combinations per fold"*, coarse
#: rather than fine. A large grid does not inflate the haircut — it inflates the
#: chance each fold overfits internally, which shows up as `wf_efficiency`
#: collapsing. That is the diagnostic to watch, not `N_trials`.
MAX_GRID_COMBINATIONS = 50

#: Every strategy is evaluated at all three train lengths and the best reported.
TRAIN_YEARS = (1, 2, 3)


class FoldWindow(NamedTuple):
    """One fold's four boundaries. A tuple so it unpacks like a coordinate."""

    train_start: date
    train_end: date
    test_start: date
    test_end: date


@dataclass(frozen=True)
class SliceOutcome:
    """What evaluating one date range produced."""

    returns: np.ndarray
    dates: np.ndarray
    n_trades: int
    #: Optional per-configuration return matrix from the fold's tuning grid,
    #: which is what CSCV needs to measure selection overfitting.
    grid_returns: np.ndarray | None = None
    #: The same series before costs, and the costs themselves. Signals never see
    #: costs, so positions do not change with the multiplier — which means the
    #: whole cost-sensitivity sweep is recoverable from these two arrays instead
    #: of re-running the walk-forward once per multiplier.
    gross_returns: np.ndarray | None = None
    costs: np.ndarray | None = None

    @property
    def n_bars(self) -> int:
        return int(self.returns.size)


#: `(start, end, params) -> SliceOutcome`. Must be picklable to run in parallel.
SliceEvaluator = Callable[[date, date, dict], SliceOutcome]


@dataclass(frozen=True)
class FoldResult:
    window: FoldWindow
    chosen_params: dict
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    returns: np.ndarray
    dates: np.ndarray
    n_trades: int
    gross_returns: np.ndarray | None = None
    costs: np.ndarray | None = None
    #: This fold's per-configuration training return matrix, carried through
    #: to `run_window` so PBO and White's RC can be computed without a second
    #: tuning pass (`_run_one_fold` is the sole place tuning happens).
    grid_returns: np.ndarray | None = field(default=None, repr=False)

    # nanoAQRL addresses the boundaries directly; keeping the names avoids an
    # adapter for the sake of one dot.
    @property
    def train_start(self) -> date:
        return self.window.train_start

    @property
    def train_end(self) -> date:
        return self.window.train_end

    @property
    def test_start(self) -> date:
        return self.window.test_start

    @property
    def test_end(self) -> date:
        return self.window.test_end

    def metrics(self) -> dict[str, Any]:
        """The per-fold row stored in `evaluations.fold_metrics`.

        Concatenation hides consistency: a strategy brilliant in 3 folds and
        terrible in 5 can still concatenate to a respectable Sharpe (TRD §8.3).
        This is how that becomes visible.
        """
        return {
            "train_start": str(self.window.train_start),
            "train_end": str(self.window.train_end),
            "test_start": str(self.window.test_start),
            "test_end": str(self.window.test_end),
            "chosen_params": {str(k): v for k, v in self.chosen_params.items()},
            "in_sample_sharpe": self.in_sample_sharpe,
            "out_of_sample_sharpe": self.out_of_sample_sharpe,
            "n_trades": self.n_trades,
            "n_bars": int(self.returns.size),
            "total_return": float(np.prod(1.0 + self.returns) - 1.0) if self.returns.size else 0.0,
        }


@dataclass(frozen=True)
class WindowResult:
    """One train-window length, scored over its concatenated folds."""

    train_years: int
    folds: list[FoldResult]
    concatenated_returns: np.ndarray
    concatenated_dates: np.ndarray
    total_trades: int
    wf_efficiency: float
    score: HonestScoreResult
    grid_returns: np.ndarray | None = field(default=None, repr=False)
    concatenated_gross: np.ndarray | None = field(default=None, repr=False)
    concatenated_costs: np.ndarray | None = field(default=None, repr=False)

    @property
    def folds_profitable(self) -> int:
        return sum(1 for fold in self.folds if fold.returns.sum() > 0.0)

    @property
    def fold_metrics(self) -> list[dict[str, Any]]:
        return [fold.metrics() for fold in self.folds]

    @property
    def tuned_params_per_fold(self) -> list[dict[str, Any]]:
        return [{str(k): v for k, v in fold.chosen_params.items()} for fold in self.folds]


@dataclass(frozen=True)
class BestOfThreeResult:
    """All three train windows, and which one won."""

    windows: dict[int, WindowResult]
    winning_train_years: int

    @property
    def winning_window(self) -> WindowResult:
        return self.windows[self.winning_train_years]

    @property
    def score(self) -> HonestScoreResult:
        return self.winning_window.score

    @property
    def score_spread(self) -> dict[int, float]:
        return {years: window.score.honest_score for years, window in self.windows.items()}

    @property
    def train_window_spread(self) -> float:
        """max − min across the three. **A diagnostic, not a gate.**

        0.61 / 0.58 / 0.60 is robust to history length; 0.62 / 0.11 / 0.09 is
        not — and that spread is a first-class diagnostic even though it does
        not gate (TRD §8.2).
        """
        scores = list(self.score_spread.values())
        return float(max(scores) - min(scores)) if scores else 0.0


# -- fold geometry ---------------------------------------------------------------


def _as_date(value: Any) -> date:
    if isinstance(value, np.datetime64):
        return value.astype("datetime64[D]").astype(date)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:19]).date()


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # 29 February in a non-leap target year
        return value.replace(year=value.year + years, day=28)


def generate_rolling_folds(
    dates: Sequence[Any],
    train_years: int,
    test_years: int,
    embargo_days: int,
) -> list[FoldWindow]:
    """Rolling, not anchored: train length is fixed and slides forward.

    Rolling over anchored because train and test lengths stay constant, so
    **folds are comparable to each other**. Anchored fails that — its later
    folds carry several times the training data of its early ones (TRD §8.1).

    The embargo is a gap between the end of training and the start of testing.
    It must be at least the strategy's holding period, or trades straddle the
    boundary and leak; the caller sizes it, this function only honours it.
    """
    if len(dates) == 0:
        return []
    data_start, data_end = _as_date(dates[0]), _as_date(dates[-1])

    folds: list[FoldWindow] = []
    start = data_start
    while True:
        train_end = _add_years(start, train_years)
        test_start = train_end + timedelta(days=embargo_days)
        test_end = _add_years(test_start, test_years)
        if test_end > data_end:
            break
        folds.append(FoldWindow(start, train_end, test_start, test_end))
        start = _add_years(start, test_years)
    return folds


# -- tuning -----------------------------------------------------------------------


def grid_combinations(
    param_grid: dict[str, list], limit: int = MAX_GRID_COMBINATIONS, base_seed: int = 0
) -> list[dict]:
    """The parameter combinations one fold tries, capped and deterministic."""
    keys = list(param_grid.keys())
    combinations = [dict(zip(keys, values)) for values in itertools.product(*param_grid.values())]
    if len(combinations) > limit:
        rng = rng_for(base_seed, "grid", tuple(sorted(keys)), len(combinations))
        chosen = rng.choice(len(combinations), size=limit, replace=False)
        combinations = [combinations[index] for index in sorted(chosen)]
    return combinations


@dataclass(frozen=True)
class _Tuning:
    params: dict
    in_sample_sharpe: float
    train_bars: int
    grid_returns: np.ndarray | None


def _tune_on_training(
    evaluate: SliceEvaluator,
    window: FoldWindow,
    base_params: dict,
    param_grid: dict[str, list] | None,
    periods_per_year: float,
    base_seed: int,
) -> _Tuning:
    """Select parameters on the training window **only**.

    This never counts toward `N_trials` (TRD §8.6): it never saw the test
    window, so it is part of the procedure being evaluated rather than a
    selection on the reported metric. If tuning ever touched the test window,
    the out-of-sample number would be fiction.

    The training bar count comes back with the choice rather than from a second
    pass. Re-running the training slice purely to measure its length would cost
    one extra backtest per fold — ~72 per experiment before the grid is counted.
    """
    if not param_grid:
        outcome = evaluate(window.train_start, window.train_end, base_params)
        return _Tuning(
            params=base_params,
            in_sample_sharpe=sharpe_ratio(outcome.returns, periods_per_year),
            train_bars=outcome.n_bars,
            grid_returns=None,
        )

    best_params, best_sharpe, train_bars = base_params, -np.inf, 0
    grid_returns: list[np.ndarray] = []
    for combination in grid_combinations(param_grid, base_seed=base_seed):
        candidate = {**base_params, **combination}
        outcome = evaluate(window.train_start, window.train_end, candidate)
        grid_returns.append(outcome.returns)
        train_bars = max(train_bars, outcome.n_bars)
        candidate_sharpe = sharpe_ratio(outcome.returns, periods_per_year)
        if candidate_sharpe > best_sharpe:
            best_params, best_sharpe = candidate, candidate_sharpe

    return _Tuning(
        params=best_params,
        in_sample_sharpe=float(best_sharpe),
        train_bars=train_bars,
        grid_returns=_align_columns(grid_returns),
    )


def _align_columns(series: list[np.ndarray]) -> np.ndarray | None:
    """Stack per-configuration return series into `(n_bars, n_configs)`."""
    usable = [candidate for candidate in series if candidate.size]
    if len(usable) < 2:
        return None
    length = min(candidate.size for candidate in usable)
    return np.column_stack([candidate[-length:] for candidate in usable])


# -- the protocol -----------------------------------------------------------------


@dataclass(frozen=True)
class _FoldJob:
    """One fold's unit of work — a plain, picklable bundle.

    `parallel.map_ordered` may run this in a separate process (TRD §9.3:
    processes, not threads), so nothing here may be a closure. `evaluate`
    itself must be picklable too — `evaluator.PanelEvaluator` is written as a
    dataclass over arrays for exactly this reason.
    """

    evaluate: SliceEvaluator
    window: FoldWindow
    base_params: dict
    param_grid: dict[str, list] | None
    periods_per_year: float
    fold_seed: int
    min_train_bars: int
    min_test_bars: int


def _run_one_fold(job: _FoldJob) -> FoldResult | None:
    """The full body of one fold: tune on train, score on test. `None` means
    the fold was too short and is dropped, exactly as the old inline loop did."""
    tuning = _tune_on_training(
        job.evaluate, job.window, job.base_params, job.param_grid, job.periods_per_year, job.fold_seed
    )
    test = job.evaluate(job.window.test_start, job.window.test_end, tuning.params)

    if tuning.train_bars < job.min_train_bars or test.n_bars < job.min_test_bars:
        return None

    return FoldResult(
        window=job.window,
        chosen_params=tuning.params,
        in_sample_sharpe=tuning.in_sample_sharpe,
        out_of_sample_sharpe=sharpe_ratio(test.returns, job.periods_per_year),
        returns=test.returns,
        dates=test.dates,
        n_trades=test.n_trades,
        gross_returns=test.gross_returns,
        costs=test.costs,
        grid_returns=tuning.grid_returns,
    )


def run_window(
    dates: Sequence[Any],
    evaluate: SliceEvaluator,
    base_params: dict,
    train_years: int,
    test_years: int,
    embargo_days: int,
    n_trials: int,
    periods_per_year: float,
    param_grid: dict[str, list] | None = None,
    z_multiplier: float = 1.65,
    min_train_bars: int = 30,
    min_test_bars: int = 5,
    base_seed: int = 0,
    workers: int = 1,
) -> WindowResult:
    """Every fold at one train length, concatenated and scored once.

    Folds are the biggest single parallelism win in the system (~24 folds x 3
    train windows, TRD §9.3) and are independent of each other by construction
    — each tunes and scores on its own date range — so `workers > 1` fans them
    out across processes with `parallel.map_ordered`, which preserves
    submission order regardless of completion order.
    """
    windows = generate_rolling_folds(dates, train_years, test_years, embargo_days)
    jobs = [
        _FoldJob(
            evaluate=evaluate,
            window=window,
            base_params=base_params,
            param_grid=param_grid,
            periods_per_year=periods_per_year,
            fold_seed=derive_seed(base_seed, "fold", train_years, index),
            min_train_bars=min_train_bars,
            min_test_bars=min_test_bars,
        )
        for index, window in enumerate(windows)
    ]
    folds = [fold for fold in map_ordered(_run_one_fold, jobs, workers=workers) if fold is not None]
    grid_matrices = [fold.grid_returns for fold in folds if fold.grid_returns is not None]

    if folds:
        # Chronological, never completion order (TRD §8.3, §9.5). Folds are
        # generated in time order, so a straight concatenation preserves it.
        concatenated = np.concatenate([fold.returns for fold in folds])
        concatenated_dates = np.concatenate([fold.dates for fold in folds])
    else:
        concatenated = np.array([])
        concatenated_dates = np.array([], dtype="datetime64[D]")

    return WindowResult(
        train_years=train_years,
        folds=folds,
        concatenated_returns=concatenated,
        concatenated_dates=concatenated_dates,
        concatenated_gross=_concatenate_optional(folds, "gross_returns"),
        concatenated_costs=_concatenate_optional(folds, "costs"),
        total_trades=sum(fold.n_trades for fold in folds),
        wf_efficiency=walk_forward_efficiency(folds),
        score=compute_honest_score(concatenated, periods_per_year, n_trials, z_multiplier),
        grid_returns=_stack_grids(grid_matrices),
    )


def walk_forward_efficiency(folds: list[FoldResult]) -> float:
    """Out-of-sample ÷ in-sample Sharpe — **the** key diagnostic (TRD §8.3).

    If out-of-sample sits far below in-sample, each fold is overfitting
    internally even though the concatenated series looks acceptable. That is
    what a too-large tuning grid costs, and it is why the grid does not inflate
    `N_trials`: the damage shows up here instead.
    """
    if not folds:
        return 0.0
    in_sample = float(np.mean([fold.in_sample_sharpe for fold in folds]))
    out_of_sample = float(np.mean([fold.out_of_sample_sharpe for fold in folds]))
    if not np.isfinite(in_sample) or in_sample == 0.0:
        return 0.0
    return out_of_sample / in_sample


def _concatenate_optional(folds: list[FoldResult], attribute: str) -> np.ndarray | None:
    """Join an optional per-fold array, or `None` if any fold lacks it."""
    parts = [getattr(fold, attribute) for fold in folds]
    if not parts or any(part is None for part in parts):
        return None
    return np.concatenate(parts)


def _stack_grids(matrices: list[np.ndarray]) -> np.ndarray | None:
    if not matrices:
        return None
    width = min(matrix.shape[1] for matrix in matrices)
    return np.vstack([matrix[:, :width] for matrix in matrices])


def run_best_of_three(
    dates: Sequence[Any],
    evaluate: SliceEvaluator,
    base_params: dict,
    holding_period_bars: int,
    n_trials: int,
    periods_per_year: float,
    param_grid: dict[str, list] | None = None,
    test_years: int = 1,
    train_years: Sequence[int] = TRAIN_YEARS,
    z_multiplier: float = 1.65,
    min_train_bars: int = 30,
    min_test_bars: int = 5,
    base_seed: int = 0,
    embargo_days: int | None = None,
    workers: int = 1,
) -> BestOfThreeResult:
    """Run all three train windows and report the best (TRD §8.2).

    `n_trials` arrives already tripled — the caller owns the family count, and
    `stats.deflated.family_trial_count` is where the ×3 is applied, so the same
    number is used for the haircut and stored on the row.

    `workers` fans out across processes at the fold level, inside each of the
    three `run_window` calls — not across the three windows themselves. Three
    windows is too coarse-grained a split when one of them may hold 24 folds;
    the win is in the finer unit (TRD §9.3).
    """
    if embargo_days is None:
        embargo_days = max(int(holding_period_bars), 1)

    windows = {
        years: run_window(
            dates,
            evaluate,
            base_params,
            years,
            test_years,
            embargo_days,
            n_trials,
            periods_per_year,
            param_grid,
            z_multiplier,
            min_train_bars,
            min_test_bars,
            base_seed,
            workers,
        )
        for years in train_years
    }
    # Ties break towards the shortest history, deterministically: `max` keeps
    # the first maximum, and the dict is built in ascending order.
    winning = max(windows, key=lambda years: windows[years].score.honest_score)
    return BestOfThreeResult(windows=windows, winning_train_years=winning)

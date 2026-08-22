"""The two things `evaluate.py` gained: the diagnostics block and the ratchet.

The diagnostics never gate, so nothing else would notice if they silently went
wrong — which is exactly why they need a check. The ratchet does gate, and it
is the one place where a wrong comparison turns "no improvement" into "keep".
"""
import numpy as np
import pytest

from nanoaqrl import evaluate
from nanoaqrl._lib import db


class _Score:
    def __init__(self, honest_score=0.6):
        self.honest_score = honest_score
        self.sr_oos = 1.2
        self.se_sr = 0.3
        self.trials_haircut = 0.1
        self.n_trials = 3


class _Window:
    """Minimal stand-in for FoldWindow — `_diagnostics` reads only the year."""

    def __init__(self, year):
        self.test_start = type("D", (), {"year": year})()


class _Fold:
    def __init__(self, year, sharpe, returns):
        self.window = _Window(year)
        self.out_of_sample_sharpe = sharpe
        self.returns = np.asarray(returns, dtype=float)


class _WindowResult:
    def __init__(self, folds, score=0.6):
        self.folds = folds
        self.concatenated_returns = np.concatenate([f.returns for f in folds])
        n_bars = sum(f.returns.size for f in folds)
        self.concatenated_dates = np.datetime64("2015-01-01") + np.arange(n_bars).astype(
            "timedelta64[D]"
        )
        self.total_trades = 120
        self.wf_efficiency = 0.8
        self.score = _Score(score)


class _Result:
    def __init__(self, win):
        self.winning_window = win
        self.winning_train_years = 2
        self.windows = {1: win, 2: win, 3: win}


def _result(fold_specs):
    return _Result(_WindowResult([_Fold(*spec) for spec in fold_specs]))


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------


def test_fold_stability_counts_and_labels():
    result = _result([
        (2016, -0.5, [0.01, -0.02]),
        (2017, 0.4, [0.01, 0.01]),
        (2018, 1.5, [0.03, 0.01]),
    ])
    d = evaluate._diagnostics(result, result.winning_window, periods_per_year=252.0)

    assert d["n_folds"] == 3
    assert d["folds_positive"] == 2
    assert d["folds_gt1"] == 1
    assert d["fold_sharpe_min"] == pytest.approx(-0.5)
    assert d["fold_sharpes"] == "2016:-0.50,2017:0.40,2018:1.50"
    # Fewer than four folds: the early/late split would overlap itself, so it
    # must stay at zero rather than report a decay computed from shared folds.
    assert d["decay_pct"] == 0.0
    assert d["early_sharpe"] == 0.0


def test_early_late_split_is_disjoint_when_folds_allow_it():
    specs = [(2010 + i, float(i), [0.01]) for i in range(6)]
    d = evaluate._diagnostics(_result(specs), _result(specs).winning_window, periods_per_year=252.0)

    assert d["n_folds"] == 6
    assert d["early_sharpe"] == pytest.approx(np.mean([0.0, 1.0, 2.0]))
    assert d["late_sharpe"] == pytest.approx(np.mean([3.0, 4.0, 5.0]))


def test_outlier_concentration_finds_the_one_bar_carrying_the_result():
    # One +10% bar against nine +0.1% bars: the headline is one bar's doing.
    returns = [0.10] + [0.001] * 9
    result = _result([(2016, 1.0, returns)])
    d = evaluate._diagnostics(result, result.winning_window, periods_per_year=252.0)

    total = sum(returns)
    assert d["best_bar_pct"] == pytest.approx(0.10 / total * 100.0)
    assert d["best_bar_pct"] > 90.0
    assert d["top_fold_pct"] == pytest.approx(100.0)  # single fold owns all of it
    assert d["profit_factor"] == 0.0  # no losing bars at all


def test_diagnostics_survive_an_empty_return_series():
    result = _result([(2016, 0.0, [])])
    d = evaluate._diagnostics(result, result.winning_window, periods_per_year=252.0)

    assert d["maxdd_pct"] == 0.0
    assert d["mc_ruin_prob"] == 0.0
    assert set(RESULTS_DIAG_KEYS) <= set(d)


RESULTS_DIAG_KEYS = [
    c for c in evaluate.RESULTS_COLUMNS if c not in ("commit", "status", "description")
]


# --------------------------------------------------------------------------
# the ratchet
# --------------------------------------------------------------------------

_OK_RETURNS = np.array([0.01] * 50)


def _check(score, threshold, ratcheted):
    return evaluate._bar_check(
        score, n_trades=evaluate.MIN_TRADES, max_dd=0.05, oos_returns=_OK_RETURNS,
        threshold=threshold, ratcheted=ratcheted,
    )


def test_first_run_clears_on_the_preregistered_floor_inclusively():
    assert _check(evaluate.MIN_HONEST_SCORE, evaluate.MIN_HONEST_SCORE, False) == (True, None)
    assert _check(evaluate.MIN_HONEST_SCORE - 1e-9, evaluate.MIN_HONEST_SCORE, False) == (False, "min_score")


def test_ratchet_requires_beating_the_baseline_not_matching_it():
    assert _check(0.71, 0.70, True) == (True, None)
    assert _check(0.70, 0.70, True) == (False, "baseline")  # a tie is not an improvement
    assert _check(0.69, 0.70, True) == (False, "baseline")


def test_validity_floors_outrank_any_score():
    """A high score does not buy its way past too few trades or a deep
    drawdown — those are checked first and are not part of the ratchet."""
    assert evaluate._bar_check(
        0.99, n_trades=evaluate.MIN_TRADES - 1, max_dd=0.01, oos_returns=_OK_RETURNS,
        threshold=0.0, ratcheted=True,
    ) == (False, "min_trades")
    assert evaluate._bar_check(
        0.99, n_trades=evaluate.MIN_TRADES, max_dd=0.99, oos_returns=_OK_RETURNS,
        threshold=0.0, ratcheted=True,
    ) == (False, "max_drawdown")
    assert evaluate._bar_check(
        0.99, n_trades=evaluate.MIN_TRADES, max_dd=0.01, oos_returns=-_OK_RETURNS,
        threshold=0.0, ratcheted=True,
    ) == (False, "cost_stress")


def test_every_ratchet_failure_reason_maps_to_the_schema_enum():
    """`complete_experiment` raises on an unmapped reason, and the ratchet
    introduced a new one."""
    for _, reason in [
        _check(0.1, 0.7, True),
        _check(0.1, 0.7, False),
    ]:
        assert reason in db.FAILURE_REASONS


# --------------------------------------------------------------------------
# results.tsv
# --------------------------------------------------------------------------


def test_wide_row_lands_under_the_right_headers(tmp_path):
    path = tmp_path / "results.tsv"
    row = evaluate._row("abc123", "keep", "note", score=0.61, plateau=0, sharpe=1.25)
    db.append_results_tsv(path, row, evaluate.RESULTS_COLUMNS)

    header, written = path.read_text().splitlines()
    columns = dict(zip(header.split("\t"), written.split("\t")))
    assert columns["commit"] == "abc123"
    assert columns["status"] == "keep"
    assert columns["score"] == "0.610000"
    assert columns["sharpe"] == "1.2500"
    assert columns["mc_ruin_prob"] == ""  # absent from the row, not misaligned


def test_a_narrow_legacy_file_is_rotated_rather_than_appended_to(tmp_path):
    """The old five-column file must not silently absorb wide rows — every
    column after the fifth would be shifted and still parse."""
    path = tmp_path / "results.tsv"
    path.write_text("commit\tscore\tn_trades\tstatus\tdescription\nold\t0.1\t5\tdiscard\tlegacy\n")

    db.append_results_tsv(path, evaluate._row("new", "keep", "note"), evaluate.RESULTS_COLUMNS)

    assert path.with_suffix(".tsv.bak").read_text().startswith("commit\tscore\tn_trades")
    lines = path.read_text().splitlines()
    assert lines[0] == "\t".join(evaluate.RESULTS_COLUMNS)
    assert len(lines) == 2 and lines[1].startswith("new\tkeep")


# --------------------------------------------------------------------------
# the ratchet's persistence — the keep path is rare, so it is the one most
# likely to be wrong at runtime and never noticed
# --------------------------------------------------------------------------


def test_baseline_advances_and_resets_the_plateau_counter(tmp_path):
    conn = db.get_connection(tmp_path / "t.db")
    strategy_id = db.get_or_create_strategy(
        conn, name="s", family="f", market="nse_equity", timeframe="daily"
    )
    experiment_id = db.insert_experiment(
        conn, strategy_id, 1, code_commit="c", wf_config_hash="h",
        eval_engine_version="v", random_seed=1,
    )

    # Never cleared: no baseline, so the pre-registered floor is the threshold.
    assert db.best_score(conn, strategy_id) is None

    assert db.record_plateau_step(conn, strategy_id, cleared=False) == 1
    assert db.record_plateau_step(conn, strategy_id, cleared=False) == 2

    db.record_improvement(conn, strategy_id, experiment_id, 0.63)
    assert db.best_score(conn, strategy_id) == pytest.approx(0.63)
    # An improvement is not a stop, so the counter starts over rather than
    # freezing at its pre-keep value.
    assert db.record_plateau_step(conn, strategy_id, cleared=True) == 0
    assert db.record_plateau_step(conn, strategy_id, cleared=False) == 1

    db.record_improvement(conn, strategy_id, experiment_id, 0.71)
    assert db.best_score(conn, strategy_id) == pytest.approx(0.71)


def test_tabs_in_a_description_cannot_shift_later_columns(tmp_path):
    path = tmp_path / "results.tsv"
    db.append_results_tsv(
        path, evaluate._row("c", "crash", "boom\there\nand here"), evaluate.RESULTS_COLUMNS
    )

    header, written = path.read_text().splitlines()
    assert len(written.split("\t")) == len(header.split("\t"))

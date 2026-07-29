"""3d — the statistical layer: deflated Sharpe, Monte Carlo, White's RC, CSCV/PBO.

Each test targets the one property that matters for that statistic's job in
the pipeline, rather than a numeric reproduction of a paper's table — the
formulas are standard, but a wrong wiring of the trial count or the resampling
scheme is what actually causes silent damage here.
"""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.eval.determinism import derive_seed, rng_for, stable_sum
from aqrl.eval.stats.cscv import probability_of_backtest_overfitting
from aqrl.eval.stats.deflated import deflated_sharpe_ratio, family_trial_count
from aqrl.eval.stats.monte_carlo import monte_carlo_paths
from aqrl.eval.stats.reality_check import whites_reality_check

PERIODS_PER_YEAR = 252.0


# -- deflated Sharpe / trial counting -----------------------------------------------


def test_family_trial_count_is_tripled_for_best_of_three():
    assert family_trial_count(prior_trials=0, iterations=1) == 3
    assert family_trial_count(prior_trials=6, iterations=1) == 21


def test_deflated_sharpe_falls_as_trials_rise():
    # A modest edge, not a screaming one — a huge SR saturates the PSR to 1.0
    # regardless of the trial count, which would hide the very effect this
    # test exists to check.
    rng = np.random.default_rng(1)
    returns = rng.normal(0.0003, 0.01, 500)
    few_trials = deflated_sharpe_ratio(returns, PERIODS_PER_YEAR, n_trials=3)
    many_trials = deflated_sharpe_ratio(returns, PERIODS_PER_YEAR, n_trials=3000)
    assert many_trials < few_trials


def test_deflated_sharpe_is_a_probability():
    rng = np.random.default_rng(2)
    returns = rng.normal(0.002, 0.01, 500)
    value = deflated_sharpe_ratio(returns, PERIODS_PER_YEAR, n_trials=30)
    assert 0.0 <= value <= 1.0


def test_deflated_sharpe_handles_short_series():
    assert deflated_sharpe_ratio(np.array([0.01, -0.01]), PERIODS_PER_YEAR, n_trials=1) == 0.0


# -- Monte Carlo ----------------------------------------------------------------------


def test_monte_carlo_percentiles_are_ordered():
    rng = np.random.default_rng(3)
    returns = rng.normal(0.0005, 0.01, 500)
    result = monte_carlo_paths(returns, replications=200, base_seed=1)
    assert result.p5_return <= result.p50_return <= result.p95_return


def test_monte_carlo_is_reproducible_given_the_same_seed():
    rng = np.random.default_rng(4)
    returns = rng.normal(0.0, 0.01, 300)
    first = monte_carlo_paths(returns, replications=100, base_seed=42)
    second = monte_carlo_paths(returns, replications=100, base_seed=42)
    assert first == second


def test_a_strategy_with_deep_drawdowns_shows_higher_ruin_probability():
    rng = np.random.default_rng(5)
    calm = rng.normal(0.001, 0.005, 400)
    wild = rng.normal(-0.002, 0.05, 400)
    calm_result = monte_carlo_paths(calm, replications=200, base_seed=9)
    wild_result = monte_carlo_paths(wild, replications=200, base_seed=9)
    assert wild_result.ruin_probability >= calm_result.ruin_probability


def test_monte_carlo_on_too_little_data_is_inert():
    result = monte_carlo_paths(np.array([0.01]), replications=100)
    assert result.replications == 0


# -- White's Reality Check --------------------------------------------------------------


def test_reality_check_high_p_value_for_pure_noise_family():
    rng = np.random.default_rng(6)
    families = [rng.normal(0.0, 0.01, 300) for _ in range(20)]
    p_value = whites_reality_check(families, replications=200, base_seed=1)
    assert p_value > 0.05


def test_reality_check_low_p_value_for_a_real_edge():
    rng = np.random.default_rng(7)
    # One candidate has a real, persistent positive mean; the rest are noise.
    families = [rng.normal(0.0, 0.01, 400) for _ in range(9)]
    families.append(rng.normal(0.01, 0.01, 400))
    p_value = whites_reality_check(families, replications=300, base_seed=2)
    assert p_value < 0.10


def test_reality_check_p_value_never_exactly_zero():
    """The finite-sample correction: with B replications the smallest
    attainable p-value is 1/(B+1)."""
    rng = np.random.default_rng(8)
    families = [rng.normal(1.0, 0.001, 200) for _ in range(3)]  # absurdly good
    p_value = whites_reality_check(families, replications=50, base_seed=3)
    assert p_value > 0.0


def test_reality_check_on_empty_family_list_is_uninformative():
    assert whites_reality_check([], replications=50) == 1.0


# -- CSCV / PBO -------------------------------------------------------------------------


def test_pbo_is_high_when_the_best_in_sample_configuration_is_arbitrary():
    """Pure noise across configurations: whichever one wins in-sample should
    be no better than median out-of-sample about half the time."""
    rng = np.random.default_rng(9)
    matrix = rng.normal(0.0, 0.01, (400, 10))
    pbo = probability_of_backtest_overfitting(matrix, chunks=16)
    # Pure noise should land near 0.5; the tolerance is wide because PBO over
    # one draw is itself a noisy statistic — this checks it isn't systematically
    # near 0 (which would mean the selection procedure looks trustworthy on
    # data with nothing to select).
    assert 0.3 < pbo <= 1.0


def test_pbo_is_low_when_one_configuration_is_genuinely_best_everywhere():
    rng = np.random.default_rng(10)
    matrix = rng.normal(0.0, 0.01, (400, 10))
    matrix[:, 0] += 0.01  # column 0 is better in every chunk, not just overall
    pbo = probability_of_backtest_overfitting(matrix, chunks=16)
    assert pbo < 0.5


def test_pbo_with_a_single_configuration_has_no_selection_to_overfit():
    matrix = np.random.default_rng(11).normal(0.0, 0.01, (400, 1))
    assert probability_of_backtest_overfitting(matrix) == 0.0


def test_pbo_is_deterministic():
    rng = np.random.default_rng(12)
    matrix = rng.normal(0.0, 0.01, (300, 20))
    first = probability_of_backtest_overfitting(matrix)
    second = probability_of_backtest_overfitting(matrix)
    assert first == second


# -- determinism ------------------------------------------------------------------------


def test_derive_seed_depends_only_on_its_labels():
    assert derive_seed(1, "fold", 1, 0) == derive_seed(1, "fold", 1, 0)
    assert derive_seed(1, "fold", 1, 0) != derive_seed(1, "fold", 1, 1)
    assert derive_seed(1, "fold", 1, 0) != derive_seed(2, "fold", 1, 0)


def test_rng_for_is_reproducible():
    first = rng_for(7, "monte_carlo", 3).normal(size=10)
    second = rng_for(7, "monte_carlo", 3).normal(size=10)
    assert np.array_equal(first, second)


def test_stable_sum_is_order_independent_of_call_but_not_of_data_order():
    values = np.array([0.1, 0.2, 0.3, -0.05])
    assert stable_sum(values) == stable_sum(values)
    assert stable_sum(values) == pytest.approx(float(values.sum()))

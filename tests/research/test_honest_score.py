import numpy as np

from aqrl.eval.stats.honest_score import (
    compute_honest_score,
    expected_max_sharpe_under_null,
    se_sharpe,
    sharpe_ratio,
)


def test_sharpe_ratio_zero_for_flat_returns():
    assert sharpe_ratio(np.zeros(100), periods_per_year=252) == 0.0


def test_sharpe_ratio_matches_hand_calculation():
    returns = np.array([0.01, -0.005, 0.02, 0.0, -0.01] * 50)
    sr = sharpe_ratio(returns, periods_per_year=252)
    expected = returns.mean() / returns.std(ddof=1) * np.sqrt(252)
    assert np.isclose(sr, expected)


def test_se_sharpe_matches_formula_for_normal_case():
    sr, skew, kurt, n = 1.0, 0.0, 3.0, 100
    se = se_sharpe(sr, skew, kurt, n)
    expected = np.sqrt((1 + sr**2 / 2 - skew * sr + (kurt - 3) / 4 * sr**2) / n)
    assert np.isclose(se, expected)


def test_se_sharpe_grows_with_negative_skew():
    """Picking up pennies in front of a steamroller: negative skew must
    inflate the standard error (TRD §7.2)."""
    se_neutral = se_sharpe(sr=1.0, skew=0.0, kurtosis=3.0, n=100)
    se_negative_skew = se_sharpe(sr=1.0, skew=-1.0, kurtosis=3.0, n=100)
    assert se_negative_skew > se_neutral


def test_se_sharpe_grows_with_fat_tails():
    se_normal = se_sharpe(sr=1.0, skew=0.0, kurtosis=3.0, n=100)
    se_fat_tailed = se_sharpe(sr=1.0, skew=0.0, kurtosis=9.0, n=100)
    assert se_fat_tailed > se_normal


def test_se_sharpe_shrinks_with_more_trades():
    """4 trades, all winners: small n must blow up the error bar (TRD §7.2)."""
    se_small_n = se_sharpe(sr=2.0, skew=0.0, kurtosis=3.0, n=4)
    se_large_n = se_sharpe(sr=2.0, skew=0.0, kurtosis=3.0, n=1000)
    assert se_small_n > se_large_n


def test_trials_haircut_zero_for_a_single_trial():
    assert expected_max_sharpe_under_null(se_sr_for_trials=0.1, n_trials=1) == 0.0


def test_trials_haircut_grows_with_more_trials():
    """Try 10 things, subtract a little; try 10,000, subtract a lot (TRD §7.2)."""
    small = expected_max_sharpe_under_null(se_sr_for_trials=0.1, n_trials=10)
    large = expected_max_sharpe_under_null(se_sr_for_trials=0.1, n_trials=10_000)
    assert 0 < small < large


def test_honest_score_decreases_as_n_trials_increases():
    rng = np.random.default_rng(0)
    returns = rng.normal(0.0006, 0.01, size=1000)
    few_trials = compute_honest_score(returns, periods_per_year=252, n_trials=1)
    many_trials = compute_honest_score(returns, periods_per_year=252, n_trials=5000)
    assert many_trials.honest_score < few_trials.honest_score


def test_honest_score_best_of_three_trial_multiplier_is_honest():
    """TRD §8.2: taking the best of 3 windows must cost 3x the haircut of a
    single fixed window at the same base iteration count."""
    rng = np.random.default_rng(1)
    returns = rng.normal(0.0006, 0.01, size=1000)
    base_iterations = 5
    single_window = compute_honest_score(returns, periods_per_year=252, n_trials=base_iterations)
    best_of_three = compute_honest_score(returns, periods_per_year=252, n_trials=base_iterations * 3)
    assert best_of_three.trials_haircut > single_window.trials_haircut
    assert best_of_three.honest_score < single_window.honest_score

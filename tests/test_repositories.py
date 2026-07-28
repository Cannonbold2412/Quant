"""The repository layer, and the queries Backend-Schema §15 says must be exact.

`family_trial_count` matters most: the deflated Sharpe is only honest if the
trial count feeding it is right, and *"how many attempts in this family?"* is
the question git cannot answer.
"""
from __future__ import annotations

import pytest

from aqrl.db.repositories import (
    VERDICT_TO_STATUS,
    EvaluationRepository,
    ExperimentRepository,
    NullWorldRunRepository,
    Repository,
    StrategyRepository,
)


@pytest.fixture
def strategies(conn) -> StrategyRepository:
    return StrategyRepository(conn)


@pytest.fixture
def experiments(conn) -> ExperimentRepository:
    return ExperimentRepository(conn)


def test_insert_mints_a_uid_and_timestamps(strategies):
    row = strategies.get(strategies.insert(name="n", family="f", market="m", timeframe="t"))
    assert len(row["uid"]) == 36
    assert row["created_at"].endswith("+00:00"), "timestamps are ISO-8601 UTC"
    assert row["updated_at"]


def test_get_or_create_is_idempotent(strategies):
    first = strategies.get_or_create("dual MA", "ma_cross", "nse_equity", "daily")
    assert strategies.get_or_create("dual MA", "ma_cross", "nse_equity", "daily") == first
    assert strategies.count() == 1


def test_family_trial_count_spans_strategies(strategies, experiments):
    """The same idea in 3 markets is 3 trials in one family, not 3 results."""
    for market in ("nse_equity", "crypto", "forex"):
        strategy_id = strategies.insert(
            name=f"ma-{market}", family="ma_cross", market=market, timeframe="daily"
        )
        experiment_id = experiments.start(strategy_id, 1, code_commit="c")
        experiments.complete(experiment_id, "evaluated", "failed")

    assert strategies.family_trial_count("ma_cross") == 3
    assert strategies.family_trial_count("other_family") == 0


def test_next_iteration_increments_per_strategy(strategies, experiments):
    strategy_id = strategies.insert(name="n", family="f", market="m", timeframe="t")
    assert experiments.next_iteration(strategy_id) == 1
    experiments.start(strategy_id, 1, code_commit="c")
    assert experiments.next_iteration(strategy_id) == 2


def test_plateau_counter_only_increments(strategies):
    strategy_id = strategies.insert(name="n", family="f", market="m", timeframe="t")
    assert strategies.record_bar_failure(strategy_id) == 1
    assert strategies.record_bar_failure(strategy_id) == 2


def test_clearing_the_bar_records_the_best_experiment(strategies, experiments):
    strategy_id = strategies.insert(name="n", family="f", market="m", timeframe="t")
    experiment_id = experiments.start(strategy_id, 1, code_commit="c")
    strategies.record_bar_clear(strategy_id, experiment_id, 0.72)
    row = strategies.get(strategy_id)
    assert row["best_experiment_id"] == experiment_id
    assert row["best_score"] == 0.72
    assert row["status"] == "pending_promotion"


@pytest.mark.parametrize("verdict,expected", VERDICT_TO_STATUS.items())
def test_verdicts_map_onto_the_canonical_status_pair(strategies, experiments, verdict, expected):
    strategy_id = strategies.insert(name=verdict, family="f", market="m", timeframe="t")
    experiment_id = experiments.start(strategy_id, 1, code_commit="c")
    experiments.complete_from_verdict(experiment_id, verdict)
    row = experiments.get(experiment_id)
    assert (row["status"], row["outcome"]) == expected


def test_unknown_verdict_is_rejected(strategies, experiments):
    strategy_id = strategies.insert(name="n", family="f", market="m", timeframe="t")
    experiment_id = experiments.start(strategy_id, 1, code_commit="c")
    with pytest.raises(ValueError, match="unknown verdict"):
        experiments.complete_from_verdict(experiment_id, "maybe")


def test_completing_an_experiment_increments_the_strategy(strategies, experiments):
    strategy_id = strategies.insert(name="n", family="f", market="m", timeframe="t")
    experiment_id = experiments.start(strategy_id, 1, code_commit="c")
    experiments.complete(experiment_id, "evaluated", "failed")
    assert strategies.get(strategy_id)["iteration_count"] == 1


def test_comparability_query(strategies, experiments):
    """Backend-Schema §15 Q2 — which stored results are still comparable."""
    strategy_id = strategies.insert(name="n", family="f", market="m", timeframe="t")
    provenance = {
        "eval_engine_version": "v1",
        "market_profile_hash": "mp",
        "timeframe_profile_hash": "tp",
        "wf_config_hash": "wf",
    }
    experiments.start(strategy_id, 1, code_commit="a", **provenance)
    experiments.start(strategy_id, 2, code_commit="b", **{**provenance, "eval_engine_version": "v2"})

    assert len(experiments.comparable_to(**provenance)) == 1
    assert experiments.mark_incomparable(eval_engine_version="v1") == 1
    assert experiments.get(1)["comparable"] == 0


def test_json_columns_round_trip(conn, strategies, experiments):
    strategy_id = strategies.insert(name="n", family="f", market="m", timeframe="t")
    experiment_id = experiments.start(strategy_id, 1, code_commit="c")
    evaluations = EvaluationRepository(conn)
    folds = [{"fold": 0, "sharpe": 0.31}, {"fold": 1, "sharpe": -0.02}]
    evaluations.insert(
        experiment_id=experiment_id,
        phase="P3",
        result="fail",
        fold_metrics=folds,
        wf_train_years_evaluated=[1, 2, 3],
    )
    row = evaluations.latest_for_experiment(experiment_id)
    assert row["fold_metrics"] == folds
    assert row["wf_train_years_evaluated"] == [1, 2, 3]


def test_booleans_are_stored_as_integers(conn, strategies):
    strategy_id = strategies.insert(
        name="n", family="f", market="m", timeframe="t", quarantined=True
    )
    assert strategies.get(strategy_id)["quarantined"] == 1


def test_find_supports_null_filters(strategies):
    strategies.insert(name="a", family="f", market="m", timeframe="t")
    strategies.insert(name="b", family="f", market="m", timeframe="t", best_score=0.6)
    assert [r["name"] for r in strategies.find(best_score=None)] == ["a"]


def test_null_world_run_derives_the_rate_and_verdict(conn):
    runs = NullWorldRunRepository(conn)
    trusted = runs.get(runs.record("permuted_returns", 40, 0, 0.31))
    assert trusted["false_discovery_rate"] == 0.0
    assert trusted["verdict"] == "pipeline_trusted"

    suspect = runs.get(runs.record("block_bootstrap", 40, 12, 0.91))
    assert suspect["false_discovery_rate"] == pytest.approx(0.3)
    assert suspect["verdict"] == "pipeline_suspect"


def test_the_generic_base_covers_untouched_tables(conn):
    """Backend-Schema §1's build-on-need principle, applied to code."""

    class JobRepository(Repository):
        table = "jobs"
        json_columns = frozenset({"payload"})

    jobs = JobRepository(conn)
    job_id = jobs.insert(job_type="EVALUATE", payload={"experiment_id": 1}, status="pending")
    assert jobs.get(job_id)["payload"] == {"experiment_id": 1}


def test_repositories_expose_no_delete():
    """Backend-Schema §0: nothing is hard-deleted."""
    assert not hasattr(Repository, "delete")

"""Route-level tests for the Stage 12 dashboard (Implementation_Plan §15).

Drives the request handlers directly — `_dispatch` below mirrors `aqrl.
dashboard.server._RequestHandler._dispatch`'s matching (regex fullmatch,
query-string parsing, form merge) without opening a socket, so this suite
runs under plain `pytest`. Every dashboard view module imports only
`aqrl.gates`/`aqrl.monitoring` (never `aqrl.orchestration.handlers` or
`aqrl.vcs` at module level), so — unlike `tests/orchestration/*` — this
whole file collects and runs on native Windows; only the two calls that
touch `vcs` at *call* time (`gates.approve`, `monitoring.retire`) are
skipped here, the same documented gap Stage 9/11 already carried.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import polars as pl
import pytest

from aqrl import gates
from aqrl.config import reset_settings_cache
from aqrl.dashboard import views  # noqa: F401 - side effect: registers every route
from aqrl.dashboard.server import ROUTES
from aqrl.data import SnapshotManager
from aqrl.db.repositories import (
    DeploymentRepository,
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    KnowledgeEntryRepository,
    PromotionRepository,
    ResearchGoalRepository,
    ResearchQuestionRepository,
    SpecRepository,
    StrategyRepository,
    ValidationFlagRepository,
)
from aqrl.operators import Node, StrategySpec
from aqrl.profiles import ProfileLoader

MARKET, TIMEFRAME, ASSET_CLASS = "nse_equity", "daily", "cash_equity"


@pytest.fixture(autouse=True)
def _point_settings_at_env(settings, monkeypatch):
    """`ProfileLoader()`/`get_settings()` deep inside `series.py`/`gates.py`/
    `monitoring.py` resolve `Settings` from the environment, not from the
    `settings` fixture object — same reasoning as `tests/orchestration/
    conftest.py`'s own `_env_settings`."""
    monkeypatch.setenv("AQRL_DB_PATH", str(settings.db_path))
    monkeypatch.setenv("AQRL_DATA_ROOT", str(settings.data_root))
    monkeypatch.setenv("AQRL_PROFILES_DIR", str(settings.profiles_dir))
    reset_settings_cache()
    yield
    reset_settings_cache()


def _dispatch(conn, method: str, path: str, form: dict | None = None):
    split = urlsplit(path)
    query_params = {key: values[0] for key, values in parse_qs(split.query).items()}
    for candidate in ROUTES:
        if candidate.method != method:
            continue
        match = candidate.pattern.fullmatch(split.path)
        if match is None:
            continue
        params = {**query_params, **match.groupdict()}
        if method == "POST" and form:
            params = {**params, **form}
        return candidate.func(conn, match, params)
    raise AssertionError(f"no route registered for {method} {path}")


def _get(conn, path):
    return _dispatch(conn, "GET", path)


def _post(conn, path, form):
    return _dispatch(conn, "POST", path, form)


# -- fixtures: a strategy that has cleared the bar, with weak evidence -------


def _crossover_spec() -> StrategySpec:
    return StrategySpec(
        entry_logic=[
            Node(id="fast", operator="ema", inputs={"series": "price.close"}, params={"span": 10}),
            Node(id="slow", operator="ema", inputs={"series": "price.close"}, params={"span": 40}),
            Node(id="e1", operator="crossover", inputs={"fast": "fast", "slow": "slow"}),
        ],
        hypothesis="Dashboard test fixture: a plain dual-EMA crossover.",
    )


def _ingest_snapshot(conn, settings, tmp_path: Path) -> int:
    rng = np.random.default_rng(0)
    dates = [dt.date(2022, 1, 3) + dt.timedelta(days=i) for i in range(400) if (dt.date(2022, 1, 3) + dt.timedelta(days=i)).weekday() < 5]
    n = len(dates)
    closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0002, 0.01, n))
    frame = pl.DataFrame(
        {
            "date": dates,
            "instrument": ["INS00"] * n,
            "open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
            "volume": [10_000.0] * n,
        }
    )
    src = tmp_path / "bars.parquet"
    frame.write_parquet(src)
    manager = SnapshotManager(conn, settings=settings, loader=ProfileLoader(settings.profiles_dir))
    return manager.ingest(src, MARKET, TIMEFRAME, ASSET_CLASS)


def _weak_promotion(conn, settings, tmp_path: Path) -> dict:
    """A strategy with a bar-clearing evaluation, a near-miss test, an
    unprofitable regime, and a pending promotion — everything the case-
    against panel is built to surface."""
    snapshot_id = _ingest_snapshot(conn, settings, tmp_path)
    strategy_id = StrategyRepository(conn).insert(
        name="Weak evidence strategy", family="weak-fam", market=MARKET, timeframe=TIMEFRAME,
        status="awaiting_human_review", iteration_count=31,
    )
    spec_id = SpecRepository(conn).insert_spec(_crossover_spec(), strategy_id)
    experiments = ExperimentRepository(conn)
    experiment_id = experiments.start(
        strategy_id, experiments.next_iteration(strategy_id),
        spec_id=spec_id, data_snapshot_id=snapshot_id, code_commit="cafef00d",
    )
    experiments.complete(experiment_id, status="evaluated", outcome="passed", phase_reached="P4")
    StrategyRepository(conn).update(strategy_id, best_experiment_id=experiment_id)

    evaluation_id = EvaluationRepository(conn).insert(
        experiment_id=experiment_id, phase="P4", result="pass", bar_result="pass",
        honest_score=0.51, sharpe=0.6, win_rate=0.5, avg_trade_return=0.001, max_drawdown=-0.2,
        n_folds=10, folds_profitable=4, cost_breakeven_multiplier=1.1,
        mc_p5_return=-0.1, mc_p50_return=0.02, mc_p95_return=0.15,
    )
    EvaluationTestRepository(conn).insert(
        evaluation_id=evaluation_id, test_name="pbo", category="robustness",
        result="warn", gating=False, value=0.349, threshold=0.35,
    )
    promotion_id = PromotionRepository(conn).insert(
        strategy_id=strategy_id, best_experiment_id=experiment_id, stage_from="research", stage_to="human_review",
        decision="approve", overfitting_risk="high", confidence=0.4, iterations_considered=31,
        requires_human_approval=True, human_decision="pending",
    )
    promotion = PromotionRepository(conn).get(promotion_id)
    return {"promotion": promotion, "strategy_id": strategy_id, "experiment_id": experiment_id}


# -- Decisions: empty queue ---------------------------------------------------


def test_empty_queue_shows_empty_state_and_worst_health_item(conn):
    strategy_id = StrategyRepository(conn).insert(
        name="Only deployment", family="fam", market=MARKET, timeframe=TIMEFRAME, status="paper_trading",
    )
    DeploymentRepository(conn).insert(
        strategy_id=strategy_id, mode="paper", status="active", current_health="red", started_at="2026-01-01T00:00:00+00:00",
    )
    response = _get(conn, "/decisions")
    assert response.status == 200
    assert "No promotions are waiting" in response.body
    assert "Most concerning deployment" in response.body
    assert "Only deployment" in response.body


def test_empty_queue_with_no_deployments_at_all_does_not_crash(conn):
    response = _get(conn, "/decisions")
    assert response.status == 200
    assert "No promotions are waiting" in response.body


# -- Decisions: review page ----------------------------------------------------


def test_case_against_renders_above_the_evidence_section(conn, settings, tmp_path):
    fixture = _weak_promotion(conn, settings, tmp_path)
    uid = fixture["promotion"]["uid"]

    response = _get(conn, f"/decisions/{uid}")
    assert response.status == 200
    body = response.body
    assert "The case against" in body
    assert body.index("The case against") < body.index("<h2>Evidence</h2>")
    # iteration count, near-miss test margin, unprofitable folds and cost
    # breakeven all surface as case-against items for this fixture.
    assert "31 iterations" in body
    assert "walk-forward folds were unprofitable" in body
    assert "assumed costs" in body


def test_review_page_query_uses_the_same_evidence_gates_evidence_renders(conn, settings, tmp_path):
    """No second evidence renderer: the review page's Evidence section and
    `gates.evidence` (what `aqrl review show` prints) must agree on what A4
    actually judged — both trace back to `promotion_evidence_sections`."""
    fixture = _weak_promotion(conn, settings, tmp_path)
    promotion = fixture["promotion"]
    cli_evidence = gates.evidence(conn, promotion)
    assert "Weak evidence strategy" in cli_evidence  # sanity: the CLI path itself works

    response = _get(conn, f"/decisions/{promotion['uid']}")
    assert "pbo" in response.body  # the same evaluation_tests row appears on both


def test_review_page_missing_promotion_is_404(conn):
    response = _get(conn, "/decisions/does-not-exist")
    assert response.status == 404


# -- Decisions: writes ----------------------------------------------------------


def test_approve_without_a_note_surfaces_as_a_form_error_and_writes_nothing(conn, settings, tmp_path):
    fixture = _weak_promotion(conn, settings, tmp_path)
    uid = fixture["promotion"]["uid"]

    with pytest.raises(ValueError, match="typed note"):
        _post(conn, f"/decisions/{uid}/approve", {"by": "kiran", "note": "   "})

    assert PromotionRepository(conn).get(fixture["promotion"]["id"])["human_decision"] == "pending"


def test_reject_writes_knowledge_and_pushes_research_questions(conn, settings, tmp_path):
    fixture = _weak_promotion(conn, settings, tmp_path)
    uid = fixture["promotion"]["uid"]

    response = _post(
        conn, f"/decisions/{uid}/reject",
        {
            "by": "kiran", "note": "overfit, too many iterations", "reason": "overfit_in_sample",
            "next_questions": "does this hold with a longer OOS window?\nretest with tighter ATR bound",
        },
    )
    assert response.status == 303
    assert response.location == "/decisions"

    strategy = StrategyRepository(conn).get(fixture["strategy_id"])
    assert strategy["status"] == "rejected"
    entries = KnowledgeEntryRepository(conn).find(strategy_id=fixture["strategy_id"])
    assert len(entries) == 1
    assert entries[0]["evidence"]["failure_reasons"] == ["overfit_in_sample"]
    questions = ResearchQuestionRepository(conn).find(origin_experiment_id=fixture["experiment_id"])
    assert len(questions) == 2


def test_defer_creates_a_research_goal_and_removes_the_item_from_the_web_queue(conn, settings, tmp_path):
    fixture = _weak_promotion(conn, settings, tmp_path)
    uid = fixture["promotion"]["uid"]
    strategy_before = StrategyRepository(conn).get(fixture["strategy_id"])

    assert "Weak evidence strategy" in _get(conn, "/decisions").body

    response = _post(conn, f"/decisions/{uid}/defer", {"by": "kiran", "note": "want more regime coverage"})
    assert response.status == 303
    assert response.location == "/decisions"

    updated = PromotionRepository(conn).get(fixture["promotion"]["id"])
    assert updated["human_decision"] == "pending"  # CHECK has no 'deferred' value
    assert updated["deferred_at"] is not None

    strategy_after = StrategyRepository(conn).get(fixture["strategy_id"])
    assert strategy_after["status"] == strategy_before["status"]  # deliberately untouched

    goals = ResearchGoalRepository(conn).find(created_by="human")
    assert len(goals) == 1
    assert goals[0]["description"] == "want more regime coverage"

    assert "Weak evidence strategy" not in _get(conn, "/decisions").body


def test_cli_review_list_and_web_decisions_agree_on_the_pending_set(conn, settings, tmp_path):
    fixture = _weak_promotion(conn, settings, tmp_path)
    cli_pending_uids = {row["uid"] for row in gates.pending(conn)}
    assert cli_pending_uids == {fixture["promotion"]["uid"]}

    web_body = _get(conn, "/decisions").body
    assert fixture["promotion"]["uid"][:8] in web_body or "Weak evidence strategy" in web_body

    # after a decision, both views agree it is gone
    _post(conn, f"/decisions/{fixture['promotion']['uid']}/defer", {"by": "kiran", "note": "n"})
    assert gates.pending(conn) == []
    assert "Weak evidence strategy" not in _get(conn, "/decisions").body


# -- Health ---------------------------------------------------------------------


def test_health_list_and_detail_render(conn):
    strategy_id = StrategyRepository(conn).insert(
        name="Health route strategy", family="fam", market=MARKET, timeframe=TIMEFRAME, status="paper_trading",
    )
    deployment_id = DeploymentRepository(conn).insert(
        strategy_id=strategy_id, mode="paper", status="active", current_health="yellow",
        started_at="2026-01-01T00:00:00+00:00", trades_required=50, trades_completed=10,
        regimes_required=["trending"], regimes_observed=[],
    )
    uid = DeploymentRepository(conn).get(deployment_id)["uid"]

    list_response = _get(conn, "/health")
    assert list_response.status == 200
    assert "Health route strategy" in list_response.body

    detail_response = _get(conn, f"/health/{uid}")
    assert detail_response.status == 200
    assert "no health check recorded yet" in detail_response.body


def test_kill_requires_matching_confirmation(conn):
    strategy_id = StrategyRepository(conn).insert(
        name="Kill route strategy", family="fam", market=MARKET, timeframe=TIMEFRAME, status="paper_trading",
    )
    deployment_id = DeploymentRepository(conn).insert(
        strategy_id=strategy_id, mode="paper", status="active", started_at="2026-01-01T00:00:00+00:00",
    )
    uid = DeploymentRepository(conn).get(deployment_id)["uid"]

    with pytest.raises(ValueError, match="does not match"):
        _post(conn, f"/health/{uid}/kill", {"by": "kiran", "reason": "test", "confirm": "wrong"})
    assert DeploymentRepository(conn).get(deployment_id)["status"] == "active"

    response = _post(conn, f"/health/{uid}/kill", {"by": "kiran", "reason": "test", "confirm": "Kill route strategy"})
    assert response.status == 303
    assert DeploymentRepository(conn).get(deployment_id)["status"] == "stopped"


# -- Pipeline ---------------------------------------------------------------------


def test_pipeline_grouping_toggle_via_query_string(conn):
    StrategyRepository(conn).insert(
        name="Pipeline strategy", family="pipe-fam", market=MARKET, timeframe=TIMEFRAME, status="evaluating",
    )
    by_strategy = _get(conn, "/pipeline")
    by_market = _get(conn, "/pipeline?group=market")
    assert by_strategy.status == 200 and by_market.status == 200
    assert "pipe-fam" in by_strategy.body
    assert MARKET in by_market.body


def test_pipeline_resolves_a_data_quality_flag(conn, settings, tmp_path):
    snapshot_id = _ingest_snapshot(conn, settings, tmp_path)
    flag_id = ValidationFlagRepository(conn).raise_flag(
        snapshot_id, "unexplained_jump", instrument="INS00", bar_date="2022-03-01",
        observed_value=-0.4, threshold=0.2, detail="test flag",
    )
    from aqrl.data import SnapshotManager as SM
    SM(conn, settings=settings, loader=ProfileLoader(settings.profiles_dir)).revalidate(snapshot_id)

    assert "test flag" in _get(conn, "/pipeline").body

    response = _post(conn, f"/pipeline/flags/{flag_id}/resolve", {"by": "kiran", "resolution": "genuine_move"})
    assert response.status == 303
    flag = ValidationFlagRepository(conn).get(flag_id)
    assert flag["resolution"] == "genuine_move"
    assert "test flag" not in _get(conn, "/pipeline").body


def test_pipeline_data_quality_queue_shows_even_with_no_strategies_yet(conn, settings, tmp_path):
    """§5.3: the queue directly gates research throughput and must be
    visible on a fresh install with data ingested but no strategy started."""
    snapshot_id = _ingest_snapshot(conn, settings, tmp_path)
    ValidationFlagRepository(conn).raise_flag(snapshot_id, "unexplained_jump", instrument="INS00", detail="early flag")
    body = _get(conn, "/pipeline").body
    assert "No strategies yet." in body
    assert "early flag" in body


def test_pipeline_resolve_rejects_an_unrecognised_resolution(conn, settings, tmp_path):
    snapshot_id = _ingest_snapshot(conn, settings, tmp_path)
    flag_id = ValidationFlagRepository(conn).raise_flag(snapshot_id, "unexplained_jump", instrument="INS00")
    with pytest.raises(ValueError, match="unrecognised resolution"):
        _post(conn, f"/pipeline/flags/{flag_id}/resolve", {"by": "kiran", "resolution": "bogus"})


# -- Laboratory / Knowledge: smoke only, empty-state paths ----------------------


def test_laboratory_and_knowledge_render_on_an_empty_database(conn):
    lab = _get(conn, "/laboratory")
    knowledge = _get(conn, "/knowledge")
    assert lab.status == 200 and knowledge.status == 200
    assert "no calibration run recorded yet" in lab.body
    assert "not measured" in lab.body
    assert "no lessons recorded yet" in knowledge.body

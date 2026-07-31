"""Stage 6's actual done-when (Implementation_Plan §9):

    "a hand-written spec runs autonomously through several below-bar
    iterations, then stops the instant one clears the bar ... without
    attempting a further iteration to chase a higher score ... and a
    deliberately-stuck spec plateaus at 5 bar failures and routes to A5."

Driven through the real job queue and real `run_job` calls — claim, run,
persist, repeat — with `ReplaySession` (A2) and `ReplayReviewSession` (A3)
standing in for Claude, exactly like Stage 5's own `test_implement_handler.py`
does for the parts of this loop it could exercise before A3 existed.

**Why a filter-gated crossover, not a plain one.** The engine's own bar
(`min_trades=100` by default) needs real separation between "clears" and
"fails below" using one FIXED snapshot across the whole loop — A2 revises the
*spec*, never the underlying market data. A plain EMA crossover's trade count
on this fixture barely moves with the spans tried; gating entries behind a
z-scored `threshold` filter node gives a knob (`window`, `upper`) that swings
trade count from "well above 100" to "just below" while keeping every variant
P0-P2-clean (a real, positive, cost-surviving edge) — so every "below-bar"
iteration in this file fails specifically at the bar (`min_trades`), the one
case that actually reaches `REVIEW`, not at P0-P2 (which never enqueues it at
all: App-Flow §6's trigger is `bar_result`, and only a fully-computed
`bar_verdict` produces one).
"""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.agents.embeddings import StubEmbedder
from aqrl.agents.session import (
    ProposedHypothesis,
    ProposedPlan,
    ProposedSpec,
    ReplayReviewSession,
    ReplaySession,
    StubHypothesisSession,
)
from aqrl.data import SnapshotManager
from aqrl.db import transaction
from aqrl.db.repositories import (
    ExperimentRepository,
    JobRepository,
    ResearchGoalRepository,
    ResearchPlanRepository,
    SpecRepository,
    StrategyRepository,
)
from aqrl.operators.spec import Node, StrategySpec
from aqrl.orchestration import states
from aqrl.orchestration.events import Event, emit
from aqrl.orchestration.handlers import generate as generate_handler
from aqrl.orchestration.handlers import implement as implement_handler
from aqrl.orchestration.handlers import review as review_handler
from aqrl.orchestration.worker import run_job
from tests.eval.conftest import bars

from .conftest import ASSET_CLASS, MARKET, TIMEFRAME

#: All P0-P2-clean; only the bar's `min_trades` item separates them (module
#: docstring). Calibrated empirically against the fixed sinusoidal panel this
#: file builds, and stable across `family_prior_trials` 0-4 and the fixed
#: `random_seed=1` every job below carries.
_CLEARS_THE_BAR = (18, 2.0)
_BELOW_THE_BAR = [
    (20, 2.0),
    (25, 1.8),
    (20, 0.5),
    (22, 2.0),
    (25, 1.9),
]


def _filtered_crossover_spec(fast: int, slow: int, *, window: int, upper: float) -> StrategySpec:
    return StrategySpec(
        entry_logic=[
            Node(id="fast", operator="ema", inputs={"series": "price.close"}, params={"span": fast}),
            Node(id="slow", operator="ema", inputs={"series": "price.close"}, params={"span": slow}),
            Node(id="e1", operator="crossover", inputs={"fast": "fast", "slow": "slow"}),
        ],
        filter_logic=[
            Node(id="z", operator="zscore", inputs={"series": "price.close"}, params={"window": window}),
            Node(
                id="f1",
                operator="threshold",
                inputs={"series": "z"},
                params={"upper": upper, "lower": -1e9, "direction": "above"},
            ),
        ],
        hypothesis=f"loop fixture: filter-gated crossover (window={window}, upper={upper})",
    )


def _proposed_spec(window: int, upper: float, *, change_summary: str) -> ProposedSpec:
    spec = _filtered_crossover_spec(3, 8, window=window, upper=upper)
    return ProposedSpec(
        entry_logic=spec.entry_logic,
        filter_logic=spec.filter_logic,
        hypothesis=spec.hypothesis,
        change_summary=change_summary,
    )


def _proposed_hypothesis(window: int, upper: float, *, name: str, family: str) -> ProposedHypothesis:
    """A1's stand-in for this fixture — same filter-gated crossover shape as
    `_proposed_spec`, plus the strategy identity A1 alone proposes (Stage 7,
    Implementation_Plan §10)."""
    spec = _filtered_crossover_spec(3, 8, window=window, upper=upper)
    return ProposedHypothesis(
        name=name,
        family=family,
        market=MARKET,
        timeframe=TIMEFRAME,
        entry_logic=spec.entry_logic,
        filter_logic=spec.filter_logic,
        hypothesis=spec.hypothesis,
        rationale="loop fixture: A1 stand-in, no contradicting lessons in this empty knowledge base",
    )


@pytest.fixture
def loop_snapshot_id(conn, settings, loader, tmp_path) -> int:
    """One fixed, filter-friendly panel for the whole loop — real edge, real
    P0-P2 pass, and enough bars that a permissive filter clears `min_trades`
    while a tighter one doesn't (module docstring)."""
    n = 2600
    t = np.arange(n)
    closes = list(100.0 * np.exp(0.006 * np.sin(2 * np.pi * t / 40)))
    frame = bars({"AAA": closes, "BBB": [c * 0.8 for c in closes]})
    path = tmp_path / "src" / "loop_bars.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)

    manager = SnapshotManager(conn, settings=settings, loader=loader)
    snapshot_id = manager.ingest(path, MARKET, TIMEFRAME, ASSET_CLASS)

    # nse_equity's profile has `universe.point_in_time_available: false`
    # (TRD §14.5 — the real collection is still open), so a real ingest can
    # never set this flag for this market. Flipping it here is a deliberate,
    # test-only bypass of that open item — exactly what `loose_bar` already
    # does for the bar's trade-count/drawdown thresholds elsewhere in this
    # suite — so this file can exercise the *loop*, not re-litigate Stage 1's
    # data-collection gap.
    conn.execute("UPDATE data_snapshots SET point_in_time_membership = 1 WHERE id = ?", (snapshot_id,))
    for instrument in ("AAA", "BBB"):
        conn.execute(
            "INSERT INTO index_membership (uid, index_name, instrument, effective_from, effective_to, created_at) "
            "VALUES (?, 'NIFTY50', ?, '1990-01-01', NULL, datetime('now'))",
            (f"loop-membership-{instrument}", instrument),
        )
    return snapshot_id


@pytest.fixture
def loop_strategy_id(conn) -> int:
    return StrategyRepository(conn).insert(
        name="loop-fixture", family="loop-fixture-family", market=MARKET, timeframe=TIMEFRAME, status="draft"
    )


@pytest.fixture
def loop_goal_id(conn) -> int:
    """A research goal for Stage 7's end of this file — A1 has no strategy
    to seed against, only a goal (App-Flow §3.1)."""
    return ResearchGoalRepository(conn).insert(
        title="loop fixture goal",
        market=MARKET,
        timeframe=TIMEFRAME,
        allocation_bucket="incremental",
        hypotheses_used=0,
        status="active",
        created_by="human",
    )


@pytest.fixture(autouse=True)
def _reset_sessions():
    implement_handler.set_session(None)
    review_handler.set_session(None)
    generate_handler.set_session(None)
    generate_handler.set_embedder(StubEmbedder())
    yield
    implement_handler.set_session(None)
    review_handler.set_session(None)
    generate_handler.set_session(None)
    generate_handler.set_embedder(None)


def _seed_first_implement_job(conn, strategy_id: int, snapshot_id: int, window: int, upper: float) -> dict:
    spec_id = SpecRepository(conn).insert_spec(
        _filtered_crossover_spec(3, 8, window=window, upper=upper), strategy_id
    )
    payload = {
        "asset_class": ASSET_CLASS,
        "spec_id": spec_id,
        "data_snapshot_id": snapshot_id,
        "cost_multiplier": 1.0,
        "random_seed": 1,  # pinned: fixed across the whole loop (module docstring)
    }
    with transaction(conn, immediate=True):
        states.transition(conn, "strategies", strategy_id, "spec_ready", actor="test")
        job_id = emit(conn, Event.SPEC_SAVED, strategy_id=strategy_id, payload=payload)
    return JobRepository(conn).get(job_id)


#: Stage 8 (A5) does not exist yet — exactly like Stage 5's own `PROMOTE` job,
#: these fail loudly with `NotImplementedHandler` rather than half-working
#: (Implementation_Plan §9's design note). The routing decision this loop
#: proves is already visible the instant one of these is enqueued; running it
#: would only demonstrate that unimplemented-handler failure, which Stage 5
#: already covers for `PROMOTE` and is not this file's concern.
_TERMINAL_UNIMPLEMENTED_JOB_TYPES = frozenset({"PROMOTE", "ARCHIVE"})


def _drain_queue(conn, *, max_jobs: int = 40) -> list[str]:
    """Claim + run every job to completion, in order, until the queue is
    empty or the next job is a terminal Stage-8 handoff. Returns the sequence
    of job_types encountered, for assertions about *what happened* — not just
    *that nothing crashed*."""
    jobs = JobRepository(conn)
    ran: list[str] = []
    for _ in range(max_jobs):
        with transaction(conn, immediate=True):
            claimed = jobs.claim("loop-test-worker", lease_seconds=120)
        if claimed is None:
            return ran
        ran.append(claimed["job_type"])
        if claimed["job_type"] in _TERMINAL_UNIMPLEMENTED_JOB_TYPES:
            return ran
        code = run_job(claimed["uid"], worker_id="loop-test-worker")
        if code != 0:
            row = jobs.get(claimed["id"])
            raise AssertionError(
                f"{claimed['job_type']} job {claimed['uid']} failed (exit {code}): "
                f"{row.get('error_message')}\n{row.get('error_trace')}"
            )
    raise AssertionError(f"queue did not drain within {max_jobs} jobs: {ran}")


# -- the first named case: stops the instant one iteration clears the bar ----


def test_stops_the_instant_one_iteration_clears_the_bar(conn, loop_strategy_id, loop_snapshot_id):
    strategy_id = loop_strategy_id
    _seed_first_implement_job(conn, strategy_id, loop_snapshot_id, *_BELOW_THE_BAR[0])

    review_handler.set_session(
        ReplayReviewSession(
            [
                ProposedPlan(
                    verdict="iterate", diagnosis="not enough trades below the filter", confidence=0.5
                ).model_dump(mode="json")
            ]
        )
    )
    implement_handler.set_session(
        ReplaySession(
            [_proposed_spec(*_CLEARS_THE_BAR, change_summary="loosened the filter").model_dump(mode="json")]
        )
    )

    ran = _drain_queue(conn)
    assert ran == ["IMPLEMENT", "EVALUATE", "REVIEW", "IMPLEMENT", "EVALUATE", "PROMOTE"]

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "pending_promotion"
    assert strategy["best_experiment_id"] is not None

    jobs = JobRepository(conn)
    assert len(jobs.find(job_type="PROMOTE")) == 1
    # The bar-clearing evaluation never enqueues REVIEW again, and A3 is
    # never asked whether to keep pushing for a higher score (App-Flow §6.1).
    assert len(jobs.find(job_type="REVIEW")) == 1  # only the first, below-bar review
    assert len(jobs.find(job_type="IMPLEMENT")) == 2  # the hand-written spec + the one iteration

    plans = ResearchPlanRepository(conn).find(strategy_id=strategy_id)
    assert len(plans) == 1
    assert plans[0]["verdict"] == "iterate"


# -- the second named case: plateaus at 5 consecutive bar failures -----------


def test_plateaus_at_five_consecutive_bar_failures(conn, loop_strategy_id, loop_snapshot_id):
    strategy_id = loop_strategy_id
    _seed_first_implement_job(conn, strategy_id, loop_snapshot_id, *_BELOW_THE_BAR[0])

    # Four ITERATE verdicts (reviewing experiments 1-4); the fifth bar failure
    # (experiment 5) hits `plateau_counter >= plateau_patience` in Python
    # before a fifth review would ever be requested (`review.py`'s stop
    # condition) — so only four fixtures are needed here.
    review_handler.set_session(
        ReplayReviewSession(
            [
                ProposedPlan(verdict="iterate", diagnosis=f"attempt {i}: still below min_trades", confidence=0.5).model_dump(
                    mode="json"
                )
                for i in range(1, 5)
            ]
        )
    )
    # Iterations 2-5 each need a distinct (still below-bar) proposed spec.
    implement_handler.set_session(
        ReplaySession(
            [
                _proposed_spec(window, upper, change_summary=f"attempt: window={window}, upper={upper}").model_dump(
                    mode="json"
                )
                for window, upper in _BELOW_THE_BAR[1:]
            ]
        )
    )

    ran = _drain_queue(conn)
    assert ran.count("IMPLEMENT") == 5
    assert ran.count("EVALUATE") == 5
    assert ran.count("REVIEW") == 5
    assert ran[-1] == "ARCHIVE"

    strategy = StrategyRepository(conn).get(strategy_id)
    assert strategy["status"] == "plateaued"
    assert strategy["plateau_counter"] == 5
    assert strategy["best_experiment_id"] is None

    plans = ResearchPlanRepository(conn).find(strategy_id=strategy_id, order_by="id")
    assert [p["verdict"] for p in plans] == ["iterate", "iterate", "iterate", "iterate", "plateau"]
    # The fifth verdict was forced in Python, with no LLM call at all.
    assert plans[4]["prompt_version"] is None

    experiments = ExperimentRepository(conn).find(strategy_id=strategy_id, order_by="iteration")
    assert len(experiments) == 5
    assert experiments[-1]["outcome"] == "plateaued"
    assert experiments[-1]["failure_reason"] == "plateaued_below_bar"

    jobs = JobRepository(conn)
    assert jobs.find(job_type="PROMOTE") == []
    archive_jobs = jobs.find(job_type="ARCHIVE")
    assert len(archive_jobs) == 1
    assert archive_jobs[0]["strategy_id"] == strategy_id


# -- Stage 7's own done-when: A1 -> A2 -> evaluate -> A3, unattended ----------


def _seed_first_generate_job(conn, goal_id: int, snapshot_id: int) -> dict:
    payload = {
        "goal_id": goal_id,
        "asset_class": ASSET_CLASS,
        "data_snapshot_id": snapshot_id,
        "cost_multiplier": 1.0,
        "random_seed": 1,  # pinned: same convention as `_seed_first_implement_job`
    }
    with transaction(conn, immediate=True):
        job_id = JobRepository(conn).enqueue("GENERATE_SPEC", payload)
    return JobRepository(conn).get(job_id)


def test_a1_generates_a_spec_that_flows_through_the_unmodified_loop(conn, loop_goal_id, loop_snapshot_id):
    """Implementation_Plan §10's done-when, driven through the real queue:
    *"A1 generates novel, non-duplicate specs ... and the full
    A1->A2->evaluate->A3 loop runs end to end unattended."* No strategy or
    spec is seeded by hand here — A1 (`StubHypothesisSession`) proposes the
    strategy's identity and its first, below-bar spec; A3
    (`ReplayReviewSession`) iterates it; A2 (`ReplaySession`) revises it into
    one that clears — all through `aqrl.orchestration.worker.run_job`,
    exactly the same `IMPLEMENT`/`EVALUATE`/`REVIEW` code this file's other
    tests already exercise, unmodified.
    """
    _seed_first_generate_job(conn, loop_goal_id, loop_snapshot_id)

    generate_handler.set_session(
        StubHypothesisSession(_proposed_hypothesis(*_BELOW_THE_BAR[0], name="a1-fixture", family="a1-fixture-family"))
    )
    review_handler.set_session(
        ReplayReviewSession(
            [
                ProposedPlan(
                    verdict="iterate", diagnosis="not enough trades below the filter", confidence=0.5
                ).model_dump(mode="json")
            ]
        )
    )
    implement_handler.set_session(
        ReplaySession(
            [_proposed_spec(*_CLEARS_THE_BAR, change_summary="loosened the filter").model_dump(mode="json")]
        )
    )

    ran = _drain_queue(conn)
    assert ran == ["GENERATE_SPEC", "IMPLEMENT", "EVALUATE", "REVIEW", "IMPLEMENT", "EVALUATE", "PROMOTE"]

    strategies = StrategyRepository(conn).find()
    assert len(strategies) == 1
    strategy = strategies[0]
    assert strategy["name"] == "a1-fixture"
    assert strategy["family"] == "a1-fixture-family"
    assert strategy["status"] == "pending_promotion"
    assert strategy["best_experiment_id"] is not None

    plans = ResearchPlanRepository(conn).find(strategy_id=strategy["id"])
    assert len(plans) == 1
    assert plans[0]["verdict"] == "iterate"

    goal = ResearchGoalRepository(conn).get(loop_goal_id)
    assert goal["hypotheses_used"] == 1

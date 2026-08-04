"""Stage 8's actual done-when (Implementation_Plan §11):

    "rejected experiments demonstrably prevent similar future proposals ...
    measured by the repeat-failure rate trending toward zero."

Driven through the real job queue and real `run_job` calls, the same way
`test_loop.py` proves Stage 6's and Stage 7's done-when: a strategy fails
below the bar, A3 rejects it, A5 archives a lesson naming the structured
failure reason, and that lesson is (a) visible in the exact brief section a
future A1 call reads (`KnowledgeEntryRepository.failure_patterns`, consumed
by `assemble_generate_brief`) and (b) counted by `repeat_failure_rate` when a
second strategy fails for the identical reason after the lesson existed.

The "a lesson written too late is never counted, and a differently-caused
failure is never counted" halves of this mechanism are unit-tested directly
against `repeat_failure_rate` in `tests/test_knowledge_repositories.py`;
this file's job is the end-to-end wiring only.
"""
from __future__ import annotations

import numpy as np
import pytest

from aqrl.agents.embeddings import StubEmbedder
from aqrl.agents.session import (
    KnowledgeEntryDraft,
    LabNotebookDraft,
    ProposedHypothesis,
    ProposedKnowledge,
    ProposedPlan,
    StubHypothesisSession,
    StubKnowledgeSession,
    StubReviewSession,
)
from aqrl.data import SnapshotManager
from aqrl.db import transaction
from aqrl.db.repositories import (
    JobRepository,
    KnowledgeEntryRepository,
    StrategyRepository,
    repeat_failure_rate,
)
from aqrl.operators.spec import Node, StrategySpec
from aqrl.orchestration.handlers import archive as archive_handler
from aqrl.orchestration.handlers import generate as generate_handler
from aqrl.orchestration.handlers import implement as implement_handler
from aqrl.orchestration.handlers import review as review_handler
from aqrl.orchestration.worker import run_job
from tests.eval.conftest import bars

from .conftest import ASSET_CLASS, MARKET, TIMEFRAME

#: Below the bar's `min_trades` on the fixed panel this file builds — same
#: calibration approach as `test_loop.py`, two distinct filter windows so
#: the two strategies below don't collide on `spec_hash`.
_STRATEGY_A_FILTER = (20, 2.0)
_STRATEGY_B_FILTER = (25, 1.8)


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
        hypothesis=f"memory-loop fixture: filter-gated crossover (window={window}, upper={upper})",
    )


def _proposed_hypothesis(window: int, upper: float, *, name: str, family: str) -> ProposedHypothesis:
    spec = _filtered_crossover_spec(3, 8, window=window, upper=upper)
    return ProposedHypothesis(
        name=name,
        family=family,
        market=MARKET,
        timeframe=TIMEFRAME,
        entry_logic=spec.entry_logic,
        filter_logic=spec.filter_logic,
        hypothesis=spec.hypothesis,
        rationale="memory-loop fixture: A1 stand-in",
    )


@pytest.fixture
def memory_snapshot_id(conn, settings, loader, tmp_path) -> int:
    """Same fixed, filter-friendly sinusoidal panel `test_loop.py` builds —
    real edge, real P0-P2 pass, a permissive filter clears `min_trades`
    while a tighter one doesn't."""
    n = 2600
    t = np.arange(n)
    closes = list(100.0 * np.exp(0.006 * np.sin(2 * np.pi * t / 40)))
    frame = bars({"AAA": closes, "BBB": [c * 0.8 for c in closes]})
    path = tmp_path / "src" / "memory_loop_bars.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)

    manager = SnapshotManager(conn, settings=settings, loader=loader)
    snapshot_id = manager.ingest(path, MARKET, TIMEFRAME, ASSET_CLASS)

    # Same test-only point-in-time bypass `test_loop.py`'s `loop_snapshot_id`
    # uses — nse_equity's real profile has no point-in-time data yet (TRD
    # §14.5), which is Stage 1's own open item, not this file's concern.
    conn.execute("UPDATE data_snapshots SET point_in_time_membership = 1 WHERE id = ?", (snapshot_id,))
    for instrument in ("AAA", "BBB"):
        conn.execute(
            "INSERT INTO index_membership (uid, index_name, instrument, effective_from, effective_to, created_at) "
            "VALUES (?, 'NIFTY50', ?, '1990-01-01', NULL, datetime('now'))",
            (f"memory-loop-membership-{instrument}", instrument),
        )
    return snapshot_id


@pytest.fixture
def memory_goal_id(conn) -> int:
    from aqrl.db.repositories import ResearchGoalRepository

    return ResearchGoalRepository(conn).insert(
        title="memory loop fixture goal",
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
    archive_handler.set_session(None)
    yield
    implement_handler.set_session(None)
    review_handler.set_session(None)
    generate_handler.set_session(None)
    generate_handler.set_embedder(None)
    archive_handler.set_session(None)


def _reject_plan(diagnosis: str) -> ProposedPlan:
    return ProposedPlan(verdict="reject", diagnosis=diagnosis, confidence=0.2)


def _knowledge_with_lesson(*, title: str, family: str) -> ProposedKnowledge:
    return ProposedKnowledge(
        notebook=LabNotebookDraft(
            hypothesis="filter-gated crossover",
            result="rejected — never enough trades past the filter",
            reason="the filter threshold is too tight for this instrument's volatility regime",
            evidence="min_trades bar failure",
            confidence=0.3,
            next_questions=["try a looser z-score window"],
        ),
        entries=[
            KnowledgeEntryDraft(
                # 'pattern', not 'lesson' — `KnowledgeEntryRepository.
                # failure_patterns` (A1's anti-amnesia reader) only surfaces
                # 'pattern'/'global_rule' entries or a 'lesson' with
                # counter-evidence (App-Flow §8.2's family-scoped-rule
                # promotion), never a plain first-sight 'lesson'.
                entry_type="pattern",
                scope="family",
                title=title,
                statement=f"{family}: tight z-score filters starve the crossover of trades",
                evidence_experiment_ids=[1],
                documents_failure_reasons=["insufficient_trades"],
                applicable_markets=[MARKET],
                applicable_timeframes=[TIMEFRAME],
                future_ideas=["widen the filter window before tightening further"],
            )
        ],
    )


def _drain_queue(conn, *, max_jobs: int = 20) -> list[str]:
    jobs = JobRepository(conn)
    ran: list[str] = []
    for _ in range(max_jobs):
        with transaction(conn, immediate=True):
            claimed = jobs.claim("memory-loop-test-worker", lease_seconds=120)
        if claimed is None:
            return ran
        ran.append(claimed["job_type"])
        code = run_job(claimed["uid"], worker_id="memory-loop-test-worker")
        if code != 0:
            row = jobs.get(claimed["id"])
            raise AssertionError(
                f"{claimed['job_type']} job {claimed['uid']} failed (exit {code}): "
                f"{row.get('error_message')}\n{row.get('error_trace')}"
            )
    raise AssertionError(f"queue did not drain within {max_jobs} jobs: {ran}")


def _run_one_strategy_to_rejection(conn, *, goal_id, snapshot_id, filt, name, family) -> int:
    """GENERATE_SPEC -> IMPLEMENT -> EVALUATE -> REVIEW(reject) -> ARCHIVE,
    all through the real queue. Returns the strategy id."""
    generate_handler.set_session(StubHypothesisSession(_proposed_hypothesis(*filt, name=name, family=family)))
    # Rejects immediately on the first below-bar evaluation — this file
    # exercises A5's write path, not A3's iterate/plateau logic (Stage 6
    # already covers that in depth).
    review_handler.set_session(
        StubReviewSession(_reject_plan("memory-loop fixture: rejecting immediately, this file tests A5, not A3"))
    )
    archive_handler.set_session(StubKnowledgeSession(_knowledge_with_lesson(title=f"{name}-lesson", family=family)))

    payload = {
        "goal_id": goal_id,
        "asset_class": ASSET_CLASS,
        "data_snapshot_id": snapshot_id,
        "cost_multiplier": 1.0,
        "random_seed": 1,
    }
    with transaction(conn, immediate=True):
        JobRepository(conn).enqueue("GENERATE_SPEC", payload)

    ran = _drain_queue(conn)
    assert ran == ["GENERATE_SPEC", "IMPLEMENT", "EVALUATE", "REVIEW", "ARCHIVE"], ran

    strategy = StrategyRepository(conn).find(name=name)[0]
    assert strategy["status"] == "rejected"
    return strategy["id"]


def test_a_rejected_experiment_writes_a_lesson_a_future_a1_brief_surfaces(conn, memory_goal_id, memory_snapshot_id):
    before = repeat_failure_rate(conn)
    assert before == {"repeats": 0, "total": 0, "rate": 0.0}

    _run_one_strategy_to_rejection(
        conn,
        goal_id=memory_goal_id,
        snapshot_id=memory_snapshot_id,
        filt=_STRATEGY_A_FILTER,
        name="memory-loop-strategy-a",
        family="memory-loop-family",
    )

    # (a) The lesson is visible through the exact reader A1's brief calls.
    patterns = KnowledgeEntryRepository(conn).failure_patterns(MARKET, TIMEFRAME)
    assert any(p["title"] == "memory-loop-strategy-a-lesson" for p in patterns)

    from aqrl.agents.context import assemble_generate_brief

    brief = assemble_generate_brief(
        goal={"title": "g", "description": "d", "market": MARKET, "timeframe": TIMEFRAME, "allocation_bucket": "incremental"},
        relevant_external_knowledge=[],
        relevant_internal_knowledge=[],
        open_questions=[],
        failure_patterns=patterns,
        family_trial_counts={},
    )
    assert "tight z-score filters starve the crossover of trades" in brief

    # First failure in an empty knowledge base: nothing to have prevented it.
    after_first = repeat_failure_rate(conn)
    assert after_first == {"repeats": 0, "total": 1, "rate": 0.0}


def test_a_second_identical_failure_after_the_lesson_is_counted_as_a_repeat(
    conn, memory_goal_id, memory_snapshot_id
):
    _run_one_strategy_to_rejection(
        conn,
        goal_id=memory_goal_id,
        snapshot_id=memory_snapshot_id,
        filt=_STRATEGY_A_FILTER,
        name="memory-loop-strategy-a2",
        family="memory-loop-family-2",
    )
    # The lesson now exists (written by strategy A's ARCHIVE, above). A
    # second, distinct strategy fails the identical way afterward — exactly
    # what the anti-amnesia machinery was supposed to prevent.
    _run_one_strategy_to_rejection(
        conn,
        goal_id=memory_goal_id,
        snapshot_id=memory_snapshot_id,
        filt=_STRATEGY_B_FILTER,
        name="memory-loop-strategy-b2",
        family="memory-loop-family-2",
    )

    result = repeat_failure_rate(conn)
    assert result["total"] == 2
    assert result["repeats"] == 1  # only the second experiment's failure was preventable
    assert result["rate"] == 0.5

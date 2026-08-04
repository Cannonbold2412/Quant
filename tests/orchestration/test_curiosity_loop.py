"""Stage 10's actual done-when (Implementation_Plan §13):

    "a failure pattern automatically produces a targeted literature search
    whose results measurably influence a subsequent hypothesis."

Driven through the real job queue and real `run_job` calls, the same way
`test_memory_loop.py` proves Stage 8's done-when: a research question is
pushed to the curiosity queue, `COLLECT_PAPERS` finds a document matching
its derived terms, `EXTRACT_KNOWLEDGE` writes an `external_knowledge` row
answering the question, that idea appears in `GENERATE_SPEC`'s brief
through the exact reader A1 calls, A1 cites it, and
`research_questions.produced_spec_ids` records the payoff —
`curiosity_payoff_rate` (Stage 10's counterpart to Stage 8's
`repeat_failure_rate`) counts it.
"""
from __future__ import annotations

from urllib.parse import quote

import pytest

from aqrl.agents.embeddings import StubEmbedder
from aqrl.agents.session import (
    ProposedExternalKnowledge,
    ProposedHypothesis,
    ReplayLibrarianSession,
    StubHypothesisSession,
)
from aqrl.db import transaction
from aqrl.db.repositories import (
    JobRepository,
    ResearchGoalRepository,
    ResearchQuestionRepository,
    SpecRepository,
    StrategyRepository,
    curiosity_payoff_rate,
)
from aqrl.librarian.collectors import _ARXIV_API
from aqrl.librarian.fetch import StubFetcher
from aqrl.librarian.relevance import keywords
from aqrl.operators.spec import Node
from aqrl.orchestration.handlers import generate as generate_handler
from aqrl.orchestration.handlers import librarian as librarian_handler
from aqrl.orchestration.worker import run_job

from .conftest import MARKET, TIMEFRAME

_QUESTION = "does volatility-adaptive momentum improve NSE equity results?"

_ARXIV_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2101.09999v1</id>
    <title>Volatility-Adaptive Momentum in Equity Markets</title>
    <summary>We show volatility-adaptive momentum improves risk-adjusted returns.</summary>
    <published>2021-02-01T00:00:00Z</published>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>
"""


@pytest.fixture(autouse=True)
def _reset_state():
    librarian_handler.set_fetcher(None)
    librarian_handler.set_session(None)
    librarian_handler.set_embedder(StubEmbedder())
    generate_handler.set_session(None)
    generate_handler.set_embedder(StubEmbedder())
    yield
    librarian_handler.set_fetcher(None)
    librarian_handler.set_session(None)
    librarian_handler.set_embedder(None)
    generate_handler.set_session(None)
    generate_handler.set_embedder(None)


def _drain_one(conn, expected_job_type: str) -> dict:
    """Claim and run exactly one job, asserting it's the type expected —
    the same shape `test_memory_loop.py`'s `_drain_queue` uses, kept
    single-step here so each stage of the loop is asserted on explicitly."""
    jobs = JobRepository(conn)
    with transaction(conn, immediate=True):
        claimed = jobs.claim("curiosity-loop-test-worker", lease_seconds=120)
    assert claimed is not None, f"expected a pending {expected_job_type!r} job, found none"
    assert claimed["job_type"] == expected_job_type
    code = run_job(claimed["uid"], worker_id="curiosity-loop-test-worker")
    row = jobs.get(claimed["id"])
    assert code == 0, f"{expected_job_type} failed: {row.get('error_message')}\n{row.get('error_trace')}"
    return row


def test_a_curiosity_search_measurably_influences_a_subsequent_hypothesis(conn):
    # 1. A failure pattern generates a targeted research question (App-Flow
    #    §8.2's mechanism — A5's own write path, exercised directly here
    #    since this file's concern is what happens AFTER the push).
    question_id = ResearchQuestionRepository(conn).push(
        _QUESTION, origin_type="experiment_failure", priority=5
    )

    before = curiosity_payoff_rate(conn)
    assert before == {"total": 1, "answered": 0, "paid_off": 0, "rate": 0.0}

    # 2. The collector runs a TARGETED search derived from the question's
    #    own text (App-Flow §12).
    terms = keywords(_QUESTION, limit=6)
    query = " AND ".join(f"all:{t}" for t in terms)
    query_url = f"{_ARXIV_API}?search_query={quote(query)}&start=0&max_results=10"
    librarian_handler.set_fetcher(StubFetcher({query_url: _ARXIV_ATOM}))

    with transaction(conn, immediate=True):
        JobRepository(conn).enqueue("COLLECT_PAPERS", {})
    _drain_one(conn, "COLLECT_PAPERS")

    question = ResearchQuestionRepository(conn).get(question_id)
    assert question["status"] == "searching"

    # 3. The Librarian extracts — one distinct idea, answering the question.
    idea = ProposedExternalKnowledge(
        source_chunk_indices=[0],
        core_idea="volatility-adaptive momentum improves risk-adjusted returns in NSE equities",
        category="signal",
        applicable_markets=[MARKET],
        applicable_timeframes=[TIMEFRAME],
        extraction_confidence=0.85,
    )
    librarian_handler.set_session(
        ReplayLibrarianSession(
            chunk_fixtures=[{"claims": ["volatility-adaptive momentum improves risk-adjusted returns"]}],
            synthesis_fixtures=[{"ideas": [idea.model_dump(mode="json")]}],
        )
    )
    _drain_one(conn, "EXTRACT_KNOWLEDGE")

    question = ResearchQuestionRepository(conn).get(question_id)
    assert question["status"] == "answered"
    assert question["answer_knowledge_ids"]
    knowledge_id = question["answer_knowledge_ids"][0]

    payoff = curiosity_payoff_rate(conn)
    assert payoff == {"total": 1, "answered": 1, "paid_off": 0, "rate": 0.0}  # answered, not yet cited

    # 4. A1's next hypothesis cites the new idea — proving it "measurably
    #    influences a subsequent hypothesis" (Stage 10's done-when), through
    #    the exact reader (`top_k_relevant`/`assemble_generate_brief`) A1
    #    actually calls, not a shortcut around it. The goal is created only
    #    now, deliberately: it didn't exist at extraction time, so the
    #    novelty push (a separate mechanism, covered in
    #    test_librarian_handler.py) has nothing to auto-target and this
    #    remains a clean single-cause chain.
    goal_id = ResearchGoalRepository(conn).insert(
        title="curiosity-loop fixture goal",
        market=MARKET,
        timeframe=TIMEFRAME,
        allocation_bucket="incremental",
        hypotheses_used=0,
        status="active",
        created_by="human",
    )
    generate_handler.set_session(
        StubHypothesisSession(
            ProposedHypothesis(
                name="curiosity-loop-strategy",
                family="curiosity-loop-family",
                market=MARKET,
                timeframe=TIMEFRAME,
                entry_logic=[
                    Node(id="fast", operator="ema", inputs={"series": "price.close"}, params={"span": 5}),
                    Node(id="slow", operator="ema", inputs={"series": "price.close"}, params={"span": 20}),
                    Node(id="e1", operator="crossover", inputs={"fast": "fast", "slow": "slow"}),
                ],
                hypothesis="volatility-adaptive momentum crossover, per the newly-collected idea",
                rationale="directly inspired by external_knowledge answering the open curiosity question",
                source_external_knowledge_ids=[knowledge_id],
            )
        )
    )
    with transaction(conn, immediate=True):
        JobRepository(conn).enqueue("GENERATE_SPEC", {"goal_id": goal_id, "asset_class": "cash_equity"})
    _drain_one(conn, "GENERATE_SPEC")

    strategy = StrategyRepository(conn).find(name="curiosity-loop-strategy")[0]
    spec = SpecRepository(conn).find(strategy_id=strategy["id"])[0]
    assert spec["source_external_knowledge_ids"] == [knowledge_id]
    assert spec["source_question_id"] == question_id  # the cycle back-edge (TRD §12.4)

    # 5. Loop closure: did asking ever pay off? Yes.
    question = ResearchQuestionRepository(conn).get(question_id)
    assert question["produced_spec_ids"] == [spec["id"]]

    after = curiosity_payoff_rate(conn)
    assert after == {"total": 1, "answered": 1, "paid_off": 1, "rate": 1.0}

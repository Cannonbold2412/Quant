"""The Librarian & Curiosity Engine handlers (Stage 10 — Implementation_Plan
§13): `COLLECT_PAPERS`'s dedup/relevance gate, `EXTRACT_KNOWLEDGE`'s
read-once idempotency and per-idea `source_chunk_ids` traceability, and
`COLLECT_MARKET_DATA`'s freshness snapshot.
"""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from aqrl.agents.embeddings import StubEmbedder
from aqrl.agents.session import ChunkExtraction, ProposedExternalKnowledge, StubLibrarianSession
from aqrl.config import reset_settings_cache
from aqrl.db import transaction
from aqrl.db.repositories import (
    AuditLogRepository,
    DocumentChunkRepository,
    ExternalDocumentRepository,
    ExternalKnowledgeRepository,
    JobRepository,
    ResearchQuestionRepository,
    SnapshotRepository,
)
from aqrl.librarian.collectors import _ARXIV_API
from aqrl.librarian.fetch import StubFetcher
from aqrl.librarian.relevance import keywords
from aqrl.orchestration.handlers import librarian as handler

_ARXIV_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2101.00001v1</id>
    <title>Volatility-Adaptive Momentum Strategies</title>
    <summary>We propose a volatility-adaptive momentum strategy for equities.</summary>
    <published>2021-01-05T00:00:00Z</published>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>
"""

_ARXIV_ATOM_IRRELEVANT = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2101.00002v1</id>
    <title>A survey of amphibian migration patterns</title>
    <summary>This paper has nothing to do with markets.</summary>
    <published>2021-01-06T00:00:00Z</published>
    <author><name>A. Frog</name></author>
  </entry>
</feed>
"""


@pytest.fixture(autouse=True)
def _reset_handler_state():
    handler.set_fetcher(None)
    handler.set_session(None)
    handler.set_embedder(None)
    yield
    handler.set_fetcher(None)
    handler.set_session(None)
    handler.set_embedder(None)


@pytest.fixture(autouse=True)
def _stub_embedder():
    handler.set_embedder(StubEmbedder())


def _arxiv_query_url(terms: list[str], *, max_results: int = 10) -> str:
    query = " AND ".join(f"all:{t}" for t in terms)
    return f"{_ARXIV_API}?search_query={quote(query)}&start=0&max_results={max_results}"


def _job(job_type: str, **payload) -> dict:
    return {"job_type": job_type, "strategy_id": None, "experiment_id": None, "payload": payload}


# -- COLLECT_PAPERS ----------------------------------------------------------------


def test_collect_broad_sweep_when_no_open_questions(conn, monkeypatch):
    monkeypatch.setenv("AQRL_LIBRARIAN_BROAD_TERMS", json.dumps(["momentum"]))
    reset_settings_cache()
    try:
        handler.set_fetcher(StubFetcher({_arxiv_query_url(["momentum"]): _ARXIV_ATOM}))

        outcome = handler.run_collect(conn, _job("COLLECT_PAPERS"))
        assert len(outcome.records) == 1
        assert outcome.consulted_questions == []

        with transaction(conn, immediate=True):
            handler.persist_collect(conn, _job("COLLECT_PAPERS"), outcome)

        documents = ExternalDocumentRepository(conn).find()
        assert len(documents) == 1
        assert documents[0]["extraction_status"] == "pending"
        assert JobRepository(conn).find(job_type="EXTRACT_KNOWLEDGE")
    finally:
        reset_settings_cache()


def test_collect_targeted_mode_consults_open_questions_and_marks_searching(conn):
    question_text = "does volatility-adaptive momentum work?"
    question_id = ResearchQuestionRepository(conn).push(question_text, origin_type="human")
    terms = keywords(question_text, limit=6)
    handler.set_fetcher(StubFetcher({_arxiv_query_url(terms): _ARXIV_ATOM}))

    outcome = handler.run_collect(conn, _job("COLLECT_PAPERS"))
    assert outcome.consulted_questions == [(question_id, terms)]
    assert len(outcome.records) == 1
    assert outcome.records[0].origin_question_id == question_id

    with transaction(conn, immediate=True):
        handler.persist_collect(conn, _job("COLLECT_PAPERS"), outcome)

    question = ResearchQuestionRepository(conn).get(question_id)
    assert question["status"] == "searching"
    assert question["search_terms"] == terms

    job = JobRepository(conn).find(job_type="EXTRACT_KNOWLEDGE")[0]
    assert job["payload"]["origin_question_id"] == question_id


def test_collect_dedupes_by_content_hash_across_runs(conn):
    question_text = "momentum question"
    ResearchQuestionRepository(conn).push(question_text, origin_type="human")
    terms = keywords(question_text, limit=6)
    handler.set_fetcher(StubFetcher({_arxiv_query_url(terms): _ARXIV_ATOM}))

    first = handler.run_collect(conn, _job("COLLECT_PAPERS"))
    with transaction(conn, immediate=True):
        handler.persist_collect(conn, _job("COLLECT_PAPERS"), first)

    second = handler.run_collect(conn, _job("COLLECT_PAPERS"))
    assert second.records == []  # already in external_documents by content_hash
    assert len(ExternalDocumentRepository(conn).find()) == 1


def test_collect_below_threshold_document_is_stored_irrelevant_not_extracted(conn, monkeypatch):
    monkeypatch.setenv("AQRL_LIBRARIAN_RELEVANCE_THRESHOLD", "0.9")
    reset_settings_cache()
    try:
        question_text = "momentum strategies"
        ResearchQuestionRepository(conn).push(question_text, origin_type="human")
        terms = keywords(question_text, limit=6)
        # The fixture paper is about amphibians — near-zero relevance to
        # "momentum"/"strategies" terms, guaranteed below a 0.9 threshold.
        handler.set_fetcher(StubFetcher({_arxiv_query_url(terms): _ARXIV_ATOM_IRRELEVANT}))
        librarian_session = StubLibrarianSession(ChunkExtraction(claims=[]), [])
        handler.set_session(librarian_session)

        outcome = handler.run_collect(conn, _job("COLLECT_PAPERS"))
        with transaction(conn, immediate=True):
            handler.persist_collect(conn, _job("COLLECT_PAPERS"), outcome)

        documents = ExternalDocumentRepository(conn).find()
        assert len(documents) == 1
        assert documents[0]["extraction_status"] == "irrelevant"
        assert JobRepository(conn).find(job_type="EXTRACT_KNOWLEDGE") == []

        # Zero LLM calls — this document never reached the Librarian at all
        # (TRD §12.2: the relevance filter runs BEFORE any LLM cost).
        assert librarian_session.chunk_calls == []
        assert librarian_session.synthesis_calls == []
    finally:
        reset_settings_cache()


# -- EXTRACT_KNOWLEDGE --------------------------------------------------------------


def _seed_document(conn, tmp_path, text: str, **overrides) -> int:
    raw_path = tmp_path / "doc.txt"
    raw_path.write_text(text, encoding="utf-8")
    fields = dict(
        source="arxiv", title="A Paper", content_hash="doc-hash", raw_path=str(raw_path),
        extraction_status="pending", relevance_score=0.9,
    )
    fields.update(overrides)
    return ExternalDocumentRepository(conn).insert(**fields)


_MULTI_SECTION_TEXT = (
    "# Signal Construction\n"
    "A volatility-scaled momentum signal outperforms a fixed-lookback one.\n\n"
    "# Risk Management\n"
    "Position sizing inversely proportional to realised volatility reduces drawdown.\n"
)


def test_extract_is_stale_for_an_already_done_document(conn, tmp_path):
    document_id = _seed_document(conn, tmp_path, _MULTI_SECTION_TEXT, extraction_status="done")
    outcome = handler.run_extract(conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id))
    assert outcome.stale is True


def test_extract_is_stale_for_an_irrelevant_document(conn, tmp_path):
    document_id = _seed_document(conn, tmp_path, _MULTI_SECTION_TEXT, extraction_status="irrelevant")
    outcome = handler.run_extract(conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id))
    assert outcome.stale is True


def test_extract_writes_chunks_and_distinct_ideas_with_traceable_source_chunk_ids(conn, tmp_path):
    document_id = _seed_document(conn, tmp_path, _MULTI_SECTION_TEXT)

    ideas = [
        ProposedExternalKnowledge(
            source_chunk_indices=[0], core_idea="volatility-scaled momentum beats fixed lookback",
            category="signal", extraction_confidence=0.9,
        ),
        ProposedExternalKnowledge(
            source_chunk_indices=[1], core_idea="inverse-volatility sizing reduces drawdown",
            category="risk_management", extraction_confidence=0.85,
        ),
    ]
    handler.set_session(
        StubLibrarianSession(lambda brief: ChunkExtraction(claims=[brief[:5]]), ideas)
    )

    outcome = handler.run_extract(conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id))
    assert outcome.stale is False
    assert len(outcome.chunks) == 2  # one per "# Heading" section
    assert len(outcome.ideas) == 2

    with transaction(conn, immediate=True):
        result = handler.persist_extract(conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id), outcome)
    assert result.tokens_spent == 0  # StubLibrarianSession never spends real tokens

    document = ExternalDocumentRepository(conn).get(document_id)
    assert document["extraction_status"] == "done"
    assert document["chunk_count"] == 2

    chunks = DocumentChunkRepository(conn).for_document(document_id)
    assert len(chunks) == 2

    knowledge_rows = ExternalKnowledgeRepository(conn).for_document(document_id)
    assert len(knowledge_rows) == 2  # one row PER IDEA, never one blob (Stage 10's second done-when)
    core_ideas = {row["core_idea"] for row in knowledge_rows}
    assert core_ideas == {
        "volatility-scaled momentum beats fixed lookback",
        "inverse-volatility sizing reduces drawdown",
    }
    for row in knowledge_rows:
        assert row["evidence_tier"] == "external_claim"
        assert row["extracted_by"] == "librarian"
        # source_chunk_ids resolves to the exact passage (Stage 10's done-when).
        chunk_ids = row["source_chunk_ids"]
        assert len(chunk_ids) == 1
        chunk = DocumentChunkRepository(conn).get(chunk_ids[0])
        assert chunk["document_id"] == document_id


def test_high_novelty_idea_triggers_the_novelty_push_for_matching_active_goals(conn, tmp_path):
    """An empty knowledge base makes every idea maximally novel (`novelty_
    score`'s own contract) — above the default threshold, so
    HIGH_NOVELTY_EXTRACTION -> GENERATE_SPEC fires (App-Flow §3.1) for every
    active goal the idea matches. `GENERATE_SPEC.run()` requires a
    `goal_id` (Implementation_Plan §10), which is why this targets goals
    rather than emitting one generic, goal-less job."""
    from aqrl.db.repositories import ResearchGoalRepository

    goal_id = ResearchGoalRepository(conn).insert(
        title="g", market="nse_equity", timeframe="daily", allocation_bucket="incremental",
        hypotheses_used=0, status="active", created_by="human",
    )
    document_id = _seed_document(conn, tmp_path, "# Only Section\nOne idea here.\n")
    idea = ProposedExternalKnowledge(source_chunk_indices=[0], core_idea="a standout idea")
    handler.set_session(StubLibrarianSession(ChunkExtraction(claims=["c"]), [idea]))

    outcome = handler.run_extract(conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id))
    with transaction(conn, immediate=True):
        handler.persist_extract(conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id), outcome)

    jobs = JobRepository(conn).find(job_type="GENERATE_SPEC")
    assert len(jobs) == 1
    assert jobs[0]["payload"]["goal_id"] == goal_id


def test_high_novelty_idea_with_no_matching_active_goal_fires_no_job(conn, tmp_path):
    """No active goal at all: the idea is still recorded, but the novelty
    push has nothing to target — it waits for a future goal or the nightly
    batch rather than crashing a `GENERATE_SPEC` job with no `goal_id`."""
    document_id = _seed_document(conn, tmp_path, "# Only Section\nOne idea here.\n")
    idea = ProposedExternalKnowledge(source_chunk_indices=[0], core_idea="a standout idea")
    handler.set_session(StubLibrarianSession(ChunkExtraction(claims=["c"]), [idea]))

    outcome = handler.run_extract(conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id))
    with transaction(conn, immediate=True):
        handler.persist_extract(conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id), outcome)

    assert JobRepository(conn).find(job_type="GENERATE_SPEC") == []
    assert ExternalKnowledgeRepository(conn).for_document(document_id)  # still recorded


def test_extract_answers_the_origin_question(conn, tmp_path):
    question_id = ResearchQuestionRepository(conn).push("q", origin_type="human")
    ResearchQuestionRepository(conn).mark_searching(question_id, search_terms=["q"])
    document_id = _seed_document(conn, tmp_path, "# Only Section\nOne idea here.\n")
    idea = ProposedExternalKnowledge(source_chunk_indices=[0], core_idea="an idea answering the question")
    handler.set_session(StubLibrarianSession(ChunkExtraction(claims=["c"]), [idea]))

    outcome = handler.run_extract(
        conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id, origin_question_id=question_id)
    )
    with transaction(conn, immediate=True):
        handler.persist_extract(
            conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id, origin_question_id=question_id), outcome
        )

    question = ResearchQuestionRepository(conn).get(question_id)
    assert question["status"] == "answered"
    assert question["answer_knowledge_ids"]


def test_extract_drops_an_idea_referencing_an_out_of_range_chunk(conn, tmp_path):
    document_id = _seed_document(conn, tmp_path, "# Only Section\nOne idea here.\n")
    bad_idea = ProposedExternalKnowledge(source_chunk_indices=[99], core_idea="references a chunk that doesn't exist")
    handler.set_session(StubLibrarianSession(ChunkExtraction(claims=["c"]), [bad_idea]))

    outcome = handler.run_extract(conn, _job("EXTRACT_KNOWLEDGE", document_id=document_id))
    assert outcome.ideas == []


# -- COLLECT_MARKET_DATA -------------------------------------------------------------


def test_market_stats_records_freshness_to_audit_log(conn):
    SnapshotRepository(conn).insert(
        market="nse_equity", timeframe="daily", asset_class="cash_equity",
        period_start="2020-01-01", period_end="2020-06-01", bar_count=100,
        storage_path="x", raw_content_hash="h" * 64, corporate_actions_version="v" * 64,
        adjustment_method="back_ratio_price", validation_status="valid",
    )
    outcome = handler.run_market_stats(conn, _job("COLLECT_MARKET_DATA"))
    assert outcome.stats
    assert outcome.stats[0]["market"] == "nse_equity"

    with transaction(conn, immediate=True):
        result = handler.persist_market_stats(conn, _job("COLLECT_MARKET_DATA"), outcome)
    assert result.tokens_spent == 0

    actions = [row["action"] for row in AuditLogRepository(conn).find()]
    assert "collect_market_data.freshness" in actions

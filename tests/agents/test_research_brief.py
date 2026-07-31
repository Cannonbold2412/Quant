"""`top_k_relevant` (Stage 7 — Implementation_Plan §10): the SQL pre-filter
bounds the candidate set, embeddings and cosine similarity rank it, all
offline via `StubEmbedder`.
"""
from __future__ import annotations

from aqrl.agents.embeddings import StubEmbedder
from aqrl.agents.research_brief import top_k_relevant
from aqrl.db import transaction
from aqrl.db.repositories import KnowledgeEntryRepository


def _seed_entries(conn, n: int) -> list[int]:
    with transaction(conn, immediate=True):
        kr = KnowledgeEntryRepository(conn)
        return [
            kr.insert(entry_type="lesson", scope="global", title=f"t{i}", statement=f"statement {i}", future_ideas=[])
            for i in range(n)
        ]


def test_returns_at_most_limit_rows(conn):
    _seed_entries(conn, 5)
    results = top_k_relevant(
        conn, StubEmbedder(), table="knowledge_entries", query_text="anything", limit=3
    )
    assert len(results) == 3


def test_empty_table_returns_empty_list(conn):
    results = top_k_relevant(conn, StubEmbedder(), table="knowledge_entries", query_text="anything")
    assert results == []


def test_results_carry_a_relevance_score(conn):
    _seed_entries(conn, 2)
    results = top_k_relevant(conn, StubEmbedder(), table="knowledge_entries", query_text="q", limit=2)
    assert all("_relevance_score" in row for row in results)
    assert all(isinstance(row["_relevance_score"], float) for row in results)


def test_results_are_sorted_descending_by_score(conn):
    _seed_entries(conn, 6)
    results = top_k_relevant(conn, StubEmbedder(), table="knowledge_entries", query_text="q", limit=6)
    scores = [row["_relevance_score"] for row in results]
    assert scores == sorted(scores, reverse=True)


def test_ranking_is_deterministic_across_calls(conn):
    """Same embedder, same query, same candidate set -> same order, every
    time — the point of a stub embedder over `numpy.random`."""
    _seed_entries(conn, 8)
    embedder = StubEmbedder()
    first = [row["id"] for row in top_k_relevant(conn, embedder, table="knowledge_entries", query_text="q", limit=8)]
    second = [row["id"] for row in top_k_relevant(conn, embedder, table="knowledge_entries", query_text="q", limit=8)]
    assert first == second


def test_candidates_are_embedded_and_cached(conn):
    _seed_entries(conn, 4)
    top_k_relevant(conn, StubEmbedder(), table="knowledge_entries", query_text="q", limit=2)
    assert conn.execute("SELECT COUNT(*) n FROM embeddings").fetchone()["n"] == 4


def test_external_knowledge_table_is_supported(conn):
    with transaction(conn, immediate=True):
        doc_id = conn.execute(
            "INSERT INTO external_documents (uid, source, content_hash, ingested_at, extraction_status) "
            "VALUES ('doc-1', 'arxiv', 'hash-1', datetime('now'), 'done')"
        ).lastrowid
        conn.execute(
            "INSERT INTO external_knowledge (uid, document_id, core_idea, novelty_score, created_at) "
            "VALUES ('ek-1', ?, 'volatility-normalized sizing', 0.8, datetime('now'))",
            (doc_id,),
        )
    results = top_k_relevant(conn, StubEmbedder(), table="external_knowledge", query_text="sizing", limit=5)
    assert len(results) == 1
    assert results[0]["core_idea"] == "volatility-normalized sizing"

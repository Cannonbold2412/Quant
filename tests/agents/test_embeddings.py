"""`StubEmbedder` and `EmbeddingRepository` (Stage 7 — Implementation_Plan
§10): deterministic, offline, cache-through — the whole reason a test suite
never needs `VOYAGE_API_KEY`.
"""
from __future__ import annotations

from aqrl.agents.embeddings import StubEmbedder, embedding_model_name
from aqrl.db import transaction
from aqrl.db.repositories import EmbeddingRepository, KnowledgeEntryRepository


def test_stub_embedder_is_deterministic():
    embedder = StubEmbedder()
    a = embedder.embed(["same text"])[0]
    b = embedder.embed(["same text"])[0]
    assert a == b


def test_stub_embedder_differs_by_text():
    embedder = StubEmbedder()
    a, b = embedder.embed(["text one", "text two"])
    assert a != b


def test_stub_embedder_dimension_is_fixed():
    embedder = StubEmbedder(dimensions=16)
    vectors = embedder.embed(["a", "b"])
    assert all(len(v) == 16 for v in vectors)


def test_embedding_model_name_is_stable_for_stub():
    embedder = StubEmbedder()
    assert embedding_model_name(embedder) == embedding_model_name(embedder)


def test_ensure_embedded_caches_and_never_re_embeds(conn):
    with transaction(conn, immediate=True):
        entry_id = KnowledgeEntryRepository(conn).insert(
            entry_type="lesson", scope="global", title="t", statement="s", future_ideas=[]
        )

    embedder = StubEmbedder()
    model = embedding_model_name(embedder)
    repo = EmbeddingRepository(conn)

    with transaction(conn, immediate=True):
        first = repo.ensure_embedded(embedder, model, "knowledge_entries", [(entry_id, "s")])
    assert conn.execute("SELECT COUNT(*) n FROM embeddings").fetchone()["n"] == 1

    with transaction(conn, immediate=True):
        second = repo.ensure_embedded(embedder, model, "knowledge_entries", [(entry_id, "s")])
    assert conn.execute("SELECT COUNT(*) n FROM embeddings").fetchone()["n"] == 1
    assert first == second


def test_ensure_embedded_only_embeds_the_missing_ones(conn):
    with transaction(conn, immediate=True):
        kr = KnowledgeEntryRepository(conn)
        id_a = kr.insert(entry_type="lesson", scope="global", title="a", statement="a", future_ideas=[])
        id_b = kr.insert(entry_type="lesson", scope="global", title="b", statement="b", future_ideas=[])

    embedder = StubEmbedder()
    model = embedding_model_name(embedder)
    repo = EmbeddingRepository(conn)

    with transaction(conn, immediate=True):
        repo.ensure_embedded(embedder, model, "knowledge_entries", [(id_a, "a")])
    with transaction(conn, immediate=True):
        both = repo.ensure_embedded(embedder, model, "knowledge_entries", [(id_a, "a"), (id_b, "b")])

    assert set(both) == {id_a, id_b}
    assert conn.execute("SELECT COUNT(*) n FROM embeddings").fetchone()["n"] == 2


def test_embed_query_is_never_cached(conn):
    embedder = StubEmbedder()
    repo = EmbeddingRepository(conn)
    repo.embed_query(embedder, "some goal description")
    assert conn.execute("SELECT COUNT(*) n FROM embeddings").fetchone()["n"] == 0

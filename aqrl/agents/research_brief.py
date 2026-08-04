"""Relevance search over the two knowledge bases — Stage 7's Research Brief
(Implementation_Plan §10, App-Flow §3.2).

**Structured filter first, embeddings second.** The worker never hands A1
"the database" — App-Flow §3.2 is explicit that this must be relevance
search, "not a full table scan." A cheap, indexed SQL query bounds the
candidate set to a few hundred rows (recency-ordered, the same shape
`KnowledgeEntryRepository.relevant_to` already uses for A3); only that
bounded set is ever embedded or ranked. At the scale this lab runs at today,
brute-force cosine similarity in Python over a few hundred vectors is the
whole "vector index" — TRD §21 marks a dedicated vector database as a Stage
13 scale-out concern, not a v1 one, and adding one now would be answering a
question nobody has asked yet.

**One real side effect, like `implement.py`'s git commit.** Embedding a
candidate row is cached (`EmbeddingRepository`, keyed on `(owner_table,
owner_id)`), so a knowledge row is embedded once, ever — not once per brief.
That cache write happens here, inside its own short transaction, exactly the
way `implement.py`'s `run()` commits to git without holding the job's
write lock: `handlers/generate.py`'s `run()` calls this module before its
own `persist()` transaction opens, so a multi-minute embedding call (like a
multi-minute Claude call) never blocks the job queue's write lock.
"""
from __future__ import annotations

import json
import math
from typing import Literal

from ..db.connection import transaction
from ..db.repositories.base import Row
from ..db.repositories.embeddings import EmbeddingRepository
from .embeddings import Embedder, embedding_model_name

__all__ = ["novelty_score", "top_k_relevant"]

KnowledgeTable = Literal["knowledge_entries", "external_knowledge"]

#: How many recency-ordered rows the SQL pre-filter admits before Python ever
#: ranks anything — the "not a table scan" bound.
_CANDIDATE_LIMIT = 300


def _candidate_rows(conn, table: KnowledgeTable, limit: int) -> list[Row]:
    if table == "knowledge_entries":
        sql = (
            "SELECT * FROM knowledge_entries WHERE superseded_by IS NULL "
            "ORDER BY id DESC LIMIT ?"
        )
    else:
        # external_knowledge carries no status column of its own (a row only
        # exists once the Librarian's synthesis pass has written it) — the
        # bound is purely recency/novelty, no extra WHERE clause needed.
        sql = "SELECT * FROM external_knowledge ORDER BY novelty_score DESC, id DESC LIMIT ?"
    return [dict(row) for row in conn.execute(sql, (limit,)).fetchall()]


def _row_text(table: KnowledgeTable, row: Row) -> str:
    if table == "knowledge_entries":
        return f"{row.get('title') or ''}\n{row.get('statement') or ''}"
    return f"{row.get('core_idea') or ''}\n{row.get('category') or ''}"


def _decode_json_columns(table: KnowledgeTable, row: Row) -> Row:
    """`_candidate_rows` reads raw `sqlite3.Row`s (a plain SQL query, not a
    `Repository.find`), so JSON columns arrive as strings here. Decode only
    the ones a brief section actually reads."""
    columns = (
        {"evidence", "applicable_markets", "applicable_timeframes", "applicable_regimes", "future_ideas"}
        if table == "knowledge_entries"
        else {"applicable_markets", "applicable_timeframes", "required_operators", "proposed_experiments"}
    )
    decoded = dict(row)
    for column in columns:
        raw = decoded.get(column)
        if isinstance(raw, str):
            try:
                decoded[column] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    return decoded


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def top_k_relevant(
    conn,
    embedder: Embedder,
    *,
    table: KnowledgeTable,
    query_text: str,
    limit: int = 10,
    candidate_limit: int = _CANDIDATE_LIMIT,
) -> list[Row]:
    """The top `limit` rows from `table` most relevant to `query_text`.

    Each returned row carries a `_relevance_score` (cosine similarity,
    higher is more relevant) alongside its own columns. Empty input, or an
    empty table, returns an empty list rather than raising.
    """
    candidates = _candidate_rows(conn, table, candidate_limit)
    if not candidates:
        return []

    model = embedding_model_name(embedder)
    repo = EmbeddingRepository(conn)
    items = [(int(row["id"]), _row_text(table, row)) for row in candidates]
    with transaction(conn, immediate=True):
        vectors = repo.ensure_embedded(embedder, model, table, items)

    query_vector = repo.embed_query(embedder, query_text)

    scored = [
        (_cosine(query_vector, vectors[int(row["id"])]), row)
        for row in candidates
        if int(row["id"]) in vectors
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    results: list[Row] = []
    for score, row in scored[:limit]:
        decoded = _decode_json_columns(table, row)
        decoded["_relevance_score"] = score
        results.append(decoded)
    return results


def novelty_score(
    conn,
    embedder: Embedder,
    text: str,
    *,
    candidate_limit: int = _CANDIDATE_LIMIT,
) -> float:
    """How much `text` differs from what the lab already knows (Backend-
    Schema §10: `external_knowledge.novelty_score`, "above a threshold,
    triggers a GENERATE_SPEC job immediately").

    `1 - max(cosine similarity)` against the same two candidate pools
    `top_k_relevant` already draws from (`external_knowledge` and
    `knowledge_entries`) — reusing `_candidate_rows`/`_cosine`/
    `EmbeddingRepository` rather than adding a second similarity code path.
    An empty knowledge base (no candidates in either table) is maximally
    novel by definition: `1.0`.
    """
    candidates = _candidate_rows(conn, "external_knowledge", candidate_limit) + _candidate_rows(
        conn, "knowledge_entries", candidate_limit
    )
    if not candidates or not text.strip():
        return 1.0

    model = embedding_model_name(embedder)
    repo = EmbeddingRepository(conn)
    by_table: dict[KnowledgeTable, list[Row]] = {"external_knowledge": [], "knowledge_entries": []}
    for row in candidates:
        by_table["external_knowledge" if "core_idea" in row else "knowledge_entries"].append(row)

    max_similarity = 0.0
    query_vector = repo.embed_query(embedder, text)
    for table, rows in by_table.items():
        if not rows:
            continue
        with transaction(conn, immediate=True):
            vectors = repo.ensure_embedded(embedder, model, table, [(int(r["id"]), _row_text(table, r)) for r in rows])
        for row in rows:
            vector = vectors.get(int(row["id"]))
            if vector is None:
                continue
            max_similarity = max(max_similarity, _cosine(query_vector, vector))

    return 1.0 - max_similarity

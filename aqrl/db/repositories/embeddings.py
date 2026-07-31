"""Cache-through storage for `agents/embeddings.py`'s vectors (Stage 7,
Implementation_Plan §10).

One row per `(owner_table, owner_id)` — a `knowledge_entries` or
`external_knowledge` row is embedded once, ever, not once per brief
assembly. `research_brief.py`'s `top_k_relevant` is the one caller.
"""
from __future__ import annotations

from ...agents.embeddings import Embedder
from .base import Repository

__all__ = ["EmbeddingRepository"]


class EmbeddingRepository(Repository):
    table = "embeddings"
    json_columns = frozenset({"vector"})
    updated_column = None

    def existing_for(self, owner_table: str, owner_ids: list[int]) -> dict[int, list[float]]:
        """Cached vectors for the given ids — a plain equality-in-list scan,
        never called against more than the candidate window
        `research_brief.top_k_relevant` already bounds via SQL."""
        if not owner_ids:
            return {}
        placeholders = ", ".join("?" for _ in owner_ids)
        rows = self.conn.execute(
            f"SELECT owner_id, vector FROM embeddings WHERE owner_table = ? AND owner_id IN ({placeholders})",
            [owner_table, *owner_ids],
        ).fetchall()
        return {row["owner_id"]: self._decode(row)["vector"] for row in rows}  # type: ignore[index]

    def ensure_embedded(
        self, embedder: Embedder, model: str, owner_table: str, items: list[tuple[int, str]]
    ) -> dict[int, list[float]]:
        """Return every item's vector, embedding (and caching) whichever ones
        aren't cached yet. One batched `embed()` call for the misses, not one
        per row — the whole reason this exists rather than a bare
        `get_or_create` loop.
        """
        if not items:
            return {}
        owner_ids = [owner_id for owner_id, _ in items]
        cached = self.existing_for(owner_table, owner_ids)

        missing = [(owner_id, text) for owner_id, text in items if owner_id not in cached]
        if not missing:
            return cached

        vectors = embedder.embed([text for _, text in missing])
        result = dict(cached)
        for (owner_id, _text), vector in zip(missing, vectors, strict=True):
            self.insert(owner_table=owner_table, owner_id=owner_id, model=model, vector=vector)
            result[owner_id] = vector
        return result

    def embed_query(self, embedder: Embedder, text: str) -> list[float]:
        """The query side of a relevance search — never cached, since a
        goal's title/description is the input, not a stored knowledge row."""
        return embedder.embed([text])[0]

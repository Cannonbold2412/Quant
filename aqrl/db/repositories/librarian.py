"""The Librarian's own tables — external documents, their chunks, and the
ideas synthesized from them (Backend-Schema §10, TRD §12.2, Stage 10).

**Read once, ever.** `ExternalDocumentRepository.by_content_hash` is the
dedup gate every collector must check before archiving raw text — the same
upsert-on-natural-key shape `KnowledgeEdgeRepository.observe` already set
for `(subject, predicate, object)`. `ExternalKnowledgeRepository.record`
enforces TRD §12.3's trust boundary in Python rather than trusting the
caller: `evidence_tier` and `extracted_by` are never accepted as arguments,
they are always what this repository writes.
"""
from __future__ import annotations

from typing import Any

from .base import Repository, Row, utcnow_iso

__all__ = ["DocumentChunkRepository", "ExternalDocumentRepository", "ExternalKnowledgeRepository"]


class ExternalDocumentRepository(Repository):
    table = "external_documents"
    # No `created_at` column on this table — `ingested_at` is its timestamp
    # (Backend-Schema §10), stamped the same way `created_at` is everywhere
    # else: in Python, at insert time.
    created_column = "ingested_at"

    def by_content_hash(self, content_hash: str) -> Row | None:
        """The dedup gate (Backend-Schema §10: `content_hash TEXT UNIQUE`).
        Checked by every collector before archiving raw text or inserting a
        row, so the same paper fetched twice (once via arXiv, once via a
        blog linking to it) never becomes two documents."""
        rows = self.find(content_hash=content_hash)
        return rows[0] if rows else None

    def pending_extraction(self, limit: int = 20) -> list[Row]:
        """Documents collected but not yet read — `EXTRACT_KNOWLEDGE`'s
        queue. Never `irrelevant`: the relevance filter already excluded
        those before any LLM cost (TRD §12.2)."""
        return self.find(extraction_status="pending", order_by="id", limit=limit)

    def mark_extracted(self, document_id: int, *, chunk_count: int) -> None:
        """A document is read exactly once, ever (TRD §12.2) — this is the
        one write that closes that door."""
        self.update(document_id, extraction_status="done", chunk_count=chunk_count, extracted_at=utcnow_iso())

    def mark_irrelevant(self, document_id: int) -> None:
        """The cheap filter's rejection, stored rather than dropped — so
        the filter's own calibration stays auditable (TRD §12.2: dedup and
        relevance filtering happen *before* any LLM cost)."""
        self.update(document_id, extraction_status="irrelevant")


class DocumentChunkRepository(Repository):
    table = "document_chunks"
    json_columns = frozenset({"chunk_extraction"})

    def for_document(self, document_id: int) -> list[Row]:
        return self.find(document_id=document_id, order_by="chunk_index")


class ExternalKnowledgeRepository(Repository):
    """The Librarian's one output shape — one row per idea, never per
    document (Backend-Schema §10)."""

    table = "external_knowledge"
    json_columns = frozenset(
        {
            "source_chunk_ids",
            "applicable_markets",
            "applicable_timeframes",
            "required_operators",
            "proposed_experiments",
            "related_knowledge_ids",
            "used_in_specs",
        }
    )

    def record(self, **fields: Any) -> int:
        """TRD §12.3's trust boundary, made structural rather than a
        convention a caller could forget: `evidence_tier` is always
        `external_claim` and `extracted_by` is always `'librarian'`,
        regardless of what (if anything) the caller passed for either —
        the schema's own CHECK only admits one `evidence_tier` value, but a
        caller-supplied `extracted_by` could still silently misattribute a
        row's provenance.
        """
        fields["evidence_tier"] = "external_claim"
        fields["extracted_by"] = "librarian"
        return self.insert(**fields)

    def for_document(self, document_id: int) -> list[Row]:
        return self.find(document_id=document_id, order_by="id")

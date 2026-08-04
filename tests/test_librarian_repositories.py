"""Stage 10's repository layer (Implementation_Plan §13): the dedup gate,
TRD §12.3's trust boundary made structural, the curiosity queue's new
`searching -> answered -> produced_spec_ids` transitions, and
`curiosity_payoff_rate`.
"""
from __future__ import annotations

from aqrl.db.repositories import (
    DocumentChunkRepository,
    ExternalDocumentRepository,
    ExternalKnowledgeRepository,
    ResearchQuestionRepository,
    curiosity_payoff_rate,
)


def _document(conn, **overrides) -> int:
    fields = dict(source="arxiv", content_hash="hash-1", extraction_status="pending")
    fields.update(overrides)
    return ExternalDocumentRepository(conn).insert(**fields)


# -- ExternalDocumentRepository --------------------------------------------------


def test_by_content_hash_is_the_dedup_gate(conn):
    documents = ExternalDocumentRepository(conn)
    doc_id = _document(conn, content_hash="abc123")
    found = documents.by_content_hash("abc123")
    assert found is not None
    assert found["id"] == doc_id
    assert documents.by_content_hash("does-not-exist") is None


def test_pending_extraction_excludes_irrelevant_and_done(conn):
    documents = ExternalDocumentRepository(conn)
    pending_id = _document(conn, content_hash="p1", extraction_status="pending")
    _document(conn, content_hash="p2", extraction_status="irrelevant")
    _document(conn, content_hash="p3", extraction_status="done")

    rows = documents.pending_extraction()
    assert [r["id"] for r in rows] == [pending_id]


def test_mark_extracted_closes_the_read_once_ever_door(conn):
    documents = ExternalDocumentRepository(conn)
    doc_id = _document(conn, content_hash="x")
    documents.mark_extracted(doc_id, chunk_count=3)
    row = documents.get(doc_id)
    assert row["extraction_status"] == "done"
    assert row["chunk_count"] == 3
    assert row["extracted_at"] is not None


def test_mark_irrelevant(conn):
    documents = ExternalDocumentRepository(conn)
    doc_id = _document(conn, content_hash="y")
    documents.mark_irrelevant(doc_id)
    assert documents.get(doc_id)["extraction_status"] == "irrelevant"


def test_ingested_at_is_stamped_without_a_created_at_column(conn):
    """`external_documents` has no `created_at` — `ingested_at` is its
    timestamp (Backend-Schema §10); `Repository.insert` must stamp that
    column instead, not fail on a nonexistent one."""
    doc_id = _document(conn, content_hash="z")
    row = ExternalDocumentRepository(conn).get(doc_id)
    assert row["ingested_at"] is not None


# -- DocumentChunkRepository ------------------------------------------------------


def test_for_document_orders_by_chunk_index(conn):
    doc_id = _document(conn, content_hash="doc-chunks")
    chunks = DocumentChunkRepository(conn)
    chunks.insert(document_id=doc_id, chunk_index=1, chunk_extraction={"claims": ["b"]})
    chunks.insert(document_id=doc_id, chunk_index=0, chunk_extraction={"claims": ["a"]})

    rows = chunks.for_document(doc_id)
    assert [r["chunk_index"] for r in rows] == [0, 1]
    assert rows[0]["chunk_extraction"] == {"claims": ["a"]}


# -- ExternalKnowledgeRepository (TRD §12.3's trust boundary) --------------------


def test_record_forces_evidence_tier_and_extracted_by(conn):
    """A caller cannot misattribute provenance, even by trying — the
    repository always writes `evidence_tier='external_claim'` and
    `extracted_by='librarian'` regardless of what (if anything) was passed."""
    doc_id = _document(conn, content_hash="idea-doc")
    knowledge = ExternalKnowledgeRepository(conn)
    knowledge_id = knowledge.record(
        document_id=doc_id,
        core_idea="volatility-normalized sizing reduces drawdown",
        evidence_tier="something_else",  # must be overridden
        extracted_by="a_human",  # must be overridden
    )
    row = knowledge.get(knowledge_id)
    assert row["evidence_tier"] == "external_claim"
    assert row["extracted_by"] == "librarian"


def test_for_document_returns_every_idea_from_that_document(conn):
    doc_id = _document(conn, content_hash="multi-idea-doc")
    knowledge = ExternalKnowledgeRepository(conn)
    knowledge.record(document_id=doc_id, core_idea="idea one")
    knowledge.record(document_id=doc_id, core_idea="idea two")

    rows = knowledge.for_document(doc_id)
    assert {r["core_idea"] for r in rows} == {"idea one", "idea two"}


# -- ResearchQuestionRepository: Stage 10's new transitions ----------------------


def _question(conn, **overrides) -> int:
    fields = dict(question="does volatility clustering help momentum?", origin_type="human")
    fields.update(overrides)
    return ResearchQuestionRepository(conn).push(**fields)


def test_mark_searching_sets_status_and_terms(conn):
    questions = ResearchQuestionRepository(conn)
    q_id = _question(conn)
    questions.mark_searching(q_id, search_terms=["volatility", "clustering"])
    row = questions.get(q_id)
    assert row["status"] == "searching"
    assert row["search_terms"] == ["volatility", "clustering"]


def test_record_answer_accumulates_knowledge_ids_and_resolves(conn):
    questions = ResearchQuestionRepository(conn)
    q_id = _question(conn)
    questions.record_answer(q_id, [1, 2])
    questions.record_answer(q_id, [2, 3])  # a second document also answers it

    row = questions.get(q_id)
    assert row["status"] == "answered"
    assert sorted(row["answer_knowledge_ids"]) == [1, 2, 3]
    assert row["resolved_at"] is not None


def test_record_answer_raises_for_unknown_question(conn):
    import pytest

    with pytest.raises(ValueError, match="no research_questions row"):
        ResearchQuestionRepository(conn).record_answer(999_999, [1])


def test_answered_by_finds_overlapping_questions_only(conn):
    questions = ResearchQuestionRepository(conn)
    answered_id = _question(conn, question="q1")
    other_id = _question(conn, question="q2")
    questions.record_answer(answered_id, [10, 11])
    questions.record_answer(other_id, [99])

    found = questions.answered_by([11, 12])
    assert [q["id"] for q in found] == [answered_id]


def test_answered_by_empty_ids_returns_empty(conn):
    assert ResearchQuestionRepository(conn).answered_by([]) == []


def test_record_produced_spec_updates_every_matching_answered_question(conn):
    """The loop-closure metric (TRD §12.4): a spec citing ideas that answer
    two different questions updates both."""
    questions = ResearchQuestionRepository(conn)
    q1 = _question(conn, question="q1")
    q2 = _question(conn, question="q2")
    unanswered = _question(conn, question="q3")
    questions.record_answer(q1, [10])
    questions.record_answer(q2, [20])

    touched = questions.record_produced_spec([10, 20, 30], spec_id=555)

    assert sorted(touched) == sorted([q1, q2])
    assert questions.get(q1)["produced_spec_ids"] == [555]
    assert questions.get(q2)["produced_spec_ids"] == [555]
    assert questions.get(unanswered)["produced_spec_ids"] is None


def test_record_produced_spec_is_idempotent(conn):
    questions = ResearchQuestionRepository(conn)
    q_id = _question(conn)
    questions.record_answer(q_id, [10])
    questions.record_produced_spec([10], spec_id=1)
    questions.record_produced_spec([10], spec_id=1)  # re-run
    assert questions.get(q_id)["produced_spec_ids"] == [1]


# -- curiosity_payoff_rate --------------------------------------------------------


def test_curiosity_payoff_rate_on_empty_table(conn):
    result = curiosity_payoff_rate(conn)
    assert result == {"total": 0, "answered": 0, "paid_off": 0, "rate": 0.0}


def test_curiosity_payoff_rate_counts_paid_off_questions(conn):
    questions = ResearchQuestionRepository(conn)
    paid_off = _question(conn, question="q1")
    answered_only = _question(conn, question="q2")
    _question(conn, question="q3")  # still open — counts toward total, not payoff

    questions.record_answer(paid_off, [10])
    questions.record_produced_spec([10], spec_id=42)
    questions.record_answer(answered_only, [20])  # answered, never cited

    result = curiosity_payoff_rate(conn)
    assert result["total"] == 3
    assert result["answered"] == 2
    assert result["paid_off"] == 1
    assert result["rate"] == 1 / 3

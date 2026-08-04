"""The Librarian & Curiosity Engine — Stage 10 (Implementation_Plan §13,
App-Flow §12, TRD §12.2). Kept **outside** the five-agent research loop
(PRD §6.4): never invoked by A3 or A4, never blocks an experiment — it only
adds candidates to the shelf `GENERATE_SPEC` reads from next cycle.

**Three job types, one module** — the `archive.py` precedent (which already
serves `ARCHIVE` and `MINE_PATTERNS`).

`COLLECT_PAPERS` is pure Python, no LLM: it runs the configured collectors
(targeted at open `research_questions` when there are any, App-Flow §12's
"targeted mode," a broad sweep otherwise), dedupes by content hash, scores
relevance **before any LLM cost**, and archives raw text to disk. Only
documents that clear `Settings.librarian_relevance_threshold` ever reach
`EXTRACT_KNOWLEDGE` — the rest are stored `irrelevant` rather than dropped,
so the filter's own calibration stays auditable.

`EXTRACT_KNOWLEDGE` is the Librarian itself: chunk by structure, pass 1 per
chunk, pass 2 synthesizes across chunks into a few distinct ideas, one
`external_knowledge` row per idea. A document is read **exactly once,
ever** — `run_extract`'s first move is checking that it has not already
been.

`COLLECT_MARKET_DATA` is scoped down from a full data-vendor pull (no
vendor is configured; Stage 1 made ingest a local-file operation) to a
freshness/coverage snapshot over already-ingested `data_snapshots` rows,
recorded to `audit_log` — a known, stated limit, not a shortcut papered
over; real regime/volatility recomputation is Stage 11's daily post-close
job once a vendor exists.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...agents.context import (
    assemble_chunk_brief,
    assemble_synthesis_brief,
    extract_chunk_prompt_version,
    extract_synthesis_prompt_version,
)
from ...agents.embeddings import Embedder
from ...agents.research_brief import novelty_score
from ...agents.session import LibrarianSession, ProposedExternalKnowledge
from ...config import get_settings
from ...db.repositories import (
    AuditLogRepository,
    DocumentChunkRepository,
    ExternalDocumentRepository,
    ExternalKnowledgeRepository,
    ResearchGoalRepository,
    ResearchQuestionRepository,
)
from ...db.repositories.base import Row, utcnow_iso
from ...hashing import hash_bytes
from ...librarian.chunking import Chunk, chunk_by_structure
from ...librarian.collectors import ArxivCollector, Collector, FeedCollector
from ...librarian.fetch import Fetcher
from ...librarian.relevance import keywords
from ...librarian.relevance import score as relevance_score
from ..budgets import consume as budget_consume
from ..events import Event, emit
from .base import HandlerResult

__all__ = [
    "CollectOutcome",
    "ExtractOutcome",
    "MarketStatsOutcome",
    "persist_collect",
    "persist_extract",
    "persist_market_stats",
    "run_collect",
    "run_extract",
    "run_market_stats",
    "set_embedder",
    "set_fetcher",
    "set_session",
]

_fetcher_override: Fetcher | None = None
_session_override: LibrarianSession | None = None
_embedder_override: Embedder | None = None


def set_fetcher(fetcher: Fetcher | None) -> None:
    """Override the fetcher `run_collect` uses. Tests only. `None` restores
    the default — a lazily-constructed `UrllibFetcher`, so importing this
    handler never touches the network unless a real collection actually
    runs."""
    global _fetcher_override
    _fetcher_override = fetcher


def _get_fetcher() -> Fetcher:
    if _fetcher_override is not None:
        return _fetcher_override
    from ...librarian.fetch import UrllibFetcher

    return UrllibFetcher()


def set_session(session: LibrarianSession | None) -> None:
    """Override the session `run_extract` calls. Tests only. `None` restores
    the default — a lazily-constructed `AnthropicSession`."""
    global _session_override
    _session_override = session


def _get_session() -> LibrarianSession:
    if _session_override is not None:
        return _session_override
    from ...agents.session import AnthropicSession

    return AnthropicSession()


def set_embedder(embedder: Embedder | None) -> None:
    """Override the embedder `novelty_score` uses. Tests only. `None`
    restores the default — a lazily-constructed `VoyageEmbedder`."""
    global _embedder_override
    _embedder_override = embedder


def _get_embedder() -> Embedder:
    if _embedder_override is not None:
        return _embedder_override
    from ...agents.embeddings import VoyageEmbedder

    return VoyageEmbedder()


# -- COLLECT_PAPERS (pure Python, no LLM) ---------------------------------------


@dataclass(frozen=True)
class CollectedRecord:
    source: str
    source_id: str
    url: str | None
    title: str
    authors: str | None
    published_at: str | None
    content_hash: str
    raw_path: str
    relevance: float
    origin_question_id: int | None


@dataclass(frozen=True)
class CollectOutcome:
    records: list[CollectedRecord] = field(default_factory=list)
    #: (question_id, terms_used) for every open question consulted this run —
    #: persisted even for one that produced nothing, so `mark_searching`
    #: reflects that it WAS looked at.
    consulted_questions: list[tuple[int, list[str]]] = field(default_factory=list)


def _search_terms_for_question(question: Row) -> list[str]:
    stored = question.get("search_terms")
    if stored:
        return list(stored)
    return keywords(question.get("question") or "", limit=6)


def _collectors(fetcher: Fetcher) -> list[Collector]:
    settings = get_settings()
    collectors: list[Collector] = [ArxivCollector(fetcher)]
    if settings.librarian_feeds:
        collectors.append(FeedCollector(fetcher, feeds=settings.librarian_feeds))
    return collectors


def _archive_raw_text(content_hash: str, text: str) -> str:
    path = get_settings().documents_root / f"{content_hash}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def run_collect(conn: sqlite3.Connection, job: Row) -> CollectOutcome:
    """The heavy, side-effect-free-on-the-database half. Archiving raw text
    to disk happens here, not in `persist` — the same real-but-idempotent
    side effect `implement.py`'s git commit and `research_brief.
    top_k_relevant`'s embedding cache already are: safe to repeat, never
    gated behind the job's write lock (module docstring)."""
    settings = get_settings()
    fetcher = _get_fetcher()
    collectors = _collectors(fetcher)

    open_questions = ResearchQuestionRepository(conn).open_questions()
    searches: list[tuple[int | None, list[str]]] = (
        [(q["id"], _search_terms_for_question(q)) for q in open_questions]
        if open_questions
        else [(None, list(settings.librarian_broad_terms))]
    )

    documents = ExternalDocumentRepository(conn)
    seen_hashes: set[str] = set()
    records: list[CollectedRecord] = []
    consulted: list[tuple[int, list[str]]] = [
        (question_id, terms) for question_id, terms in searches if question_id is not None
    ]

    for question_id, terms in searches:
        if not terms or len(records) >= settings.librarian_max_documents_per_run:
            continue
        for collector in collectors:
            for doc in collector.search(terms):
                if len(records) >= settings.librarian_max_documents_per_run:
                    break
                if not doc.text or not doc.text.strip():
                    continue
                digest = hash_bytes(doc.text.encode("utf-8"))
                if digest in seen_hashes or documents.by_content_hash(digest) is not None:
                    continue  # read once, ever — dedup before any archiving
                seen_hashes.add(digest)
                relevance = relevance_score(title=doc.title, abstract=doc.text[:2000], terms=terms)
                records.append(
                    CollectedRecord(
                        source=doc.source,
                        source_id=doc.source_id,
                        url=doc.url,
                        title=doc.title,
                        authors=doc.authors,
                        published_at=doc.published_at,
                        content_hash=digest,
                        raw_path=_archive_raw_text(digest, doc.text),
                        relevance=relevance,
                        origin_question_id=question_id,
                    )
                )

    return CollectOutcome(records=records, consulted_questions=consulted)


def persist_collect(conn: sqlite3.Connection, job: Row, outcome: CollectOutcome) -> HandlerResult:
    """The short, atomic half: every database write for `COLLECT_PAPERS`."""
    questions = ResearchQuestionRepository(conn)
    for question_id, terms in outcome.consulted_questions:
        questions.mark_searching(question_id, search_terms=terms)

    documents = ExternalDocumentRepository(conn)
    threshold = get_settings().librarian_relevance_threshold
    for record in outcome.records:
        if documents.by_content_hash(record.content_hash) is not None:
            continue  # another job archived the same content between run() and here
        is_relevant = record.relevance >= threshold
        document_id = documents.insert(
            source=record.source,
            source_id=record.source_id,
            url=record.url,
            title=record.title,
            authors=record.authors,
            published_at=record.published_at,
            content_hash=record.content_hash,
            raw_path=record.raw_path,
            extraction_status="pending" if is_relevant else "irrelevant",
            relevance_score=record.relevance,
        )
        if is_relevant:
            emit(
                conn,
                Event.DOCUMENT_INGESTED,
                payload={"document_id": document_id, "origin_question_id": record.origin_question_id},
                dedupe_key=f"document_ingested:{document_id}",
            )

    return HandlerResult(tokens_spent=0)


# -- EXTRACT_KNOWLEDGE (the Librarian itself) -----------------------------------


@dataclass(frozen=True)
class ExtractedIdea:
    proposal: ProposedExternalKnowledge
    chunk_indices: list[int]  # validated in-range positions into `ExtractOutcome.chunks`
    novelty: float


@dataclass(frozen=True)
class ExtractOutcome:
    stale: bool
    document_id: int
    chunks: list[Chunk] = field(default_factory=list)
    claims_by_chunk: list[list[str]] = field(default_factory=list)
    ideas: list[ExtractedIdea] = field(default_factory=list)
    chunk_prompt_version: str | None = None
    synthesis_prompt_version: str | None = None
    tokens_spent: int = 0
    origin_question_id: int | None = None


def _matching_active_goals(conn: sqlite3.Connection, proposal: ProposedExternalKnowledge) -> list[Row]:
    """Active `research_goals` the novelty push should target — any active
    goal if the idea named no markets/timeframes of its own, otherwise only
    goals whose market/timeframe the idea claims to apply to."""
    goals = ResearchGoalRepository(conn).active_with_budget()
    markets = set(proposal.applicable_markets)
    timeframes = set(proposal.applicable_timeframes)
    return [
        goal
        for goal in goals
        if (not markets or goal.get("market") in markets) and (not timeframes or goal.get("timeframe") in timeframes)
    ]


def run_extract(conn: sqlite3.Connection, job: Row) -> ExtractOutcome:
    """The heavy, side-effect-free-on-the-database half: two passes of LLM
    calls plus the novelty-scoring embedding calls, writes nothing."""
    payload = job["payload"] or {}
    document_id = payload.get("document_id")
    if document_id is None:
        raise ValueError("EXTRACT_KNOWLEDGE job has no 'document_id' in its payload")

    documents = ExternalDocumentRepository(conn)
    document = documents.get(document_id)
    if document is None:
        raise ValueError(f"no external_documents row {document_id}")

    # A document is read exactly once, ever (TRD §12.2) — a second
    # EXTRACT_KNOWLEDGE for an already-processed or already-rejected
    # document is a no-op, the same idempotency shape `archive.py`'s
    # `run_archive` uses.
    if document["extraction_status"] in ("done", "irrelevant"):
        return ExtractOutcome(stale=True, document_id=document_id)

    raw_path = document.get("raw_path")
    if not raw_path or not Path(raw_path).exists():
        raise ValueError(f"external_documents {document_id} has no archived raw text at {raw_path!r}")
    text = Path(raw_path).read_text(encoding="utf-8")

    settings = get_settings()
    chunks = chunk_by_structure(text)[: settings.librarian_max_chunks_per_document]
    if not chunks:
        return ExtractOutcome(stale=False, document_id=document_id)

    session = _get_session()
    chunk_prompt_version = extract_chunk_prompt_version()
    claims_by_chunk: list[list[str]] = []
    tokens_spent = 0
    for chunk in chunks:
        brief = assemble_chunk_brief(document=document, chunk=chunk)
        response = session.extract_chunk(brief, prompt_version=chunk_prompt_version)
        claims_by_chunk.append(response.extraction.claims)
        tokens_spent += response.tokens_spent

    synthesis_prompt_version = extract_synthesis_prompt_version()
    synthesis_brief = assemble_synthesis_brief(document=document, chunks=chunks, claims_by_chunk=claims_by_chunk)
    synthesis_response = session.synthesize(synthesis_brief, prompt_version=synthesis_prompt_version)
    tokens_spent += synthesis_response.tokens_spent

    embedder = _get_embedder()
    ideas: list[ExtractedIdea] = []
    for proposal in synthesis_response.ideas:
        valid_indices = [i for i in proposal.source_chunk_indices if 0 <= i < len(chunks)]
        if not valid_indices:
            continue  # a malformed reference to a chunk that doesn't exist — drop, don't guess
        ideas.append(
            ExtractedIdea(
                proposal=proposal,
                chunk_indices=valid_indices,
                novelty=novelty_score(conn, embedder, proposal.core_idea),
            )
        )

    return ExtractOutcome(
        stale=False,
        document_id=document_id,
        chunks=chunks,
        claims_by_chunk=claims_by_chunk,
        ideas=ideas,
        chunk_prompt_version=chunk_prompt_version,
        synthesis_prompt_version=synthesis_prompt_version,
        tokens_spent=tokens_spent,
        origin_question_id=payload.get("origin_question_id"),
    )


def persist_extract(conn: sqlite3.Connection, job: Row, outcome: ExtractOutcome) -> HandlerResult:
    """The short, atomic half: every database write for `EXTRACT_KNOWLEDGE`."""
    if outcome.stale:
        return HandlerResult(tokens_spent=0)

    documents = ExternalDocumentRepository(conn)
    chunk_repo = DocumentChunkRepository(conn)
    knowledge_repo = ExternalKnowledgeRepository(conn)

    chunk_ids: list[int] = [
        chunk_repo.insert(
            document_id=outcome.document_id,
            chunk_index=chunk.index,
            section_title=chunk.section_title,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            chunk_extraction={"claims": claims},
            processed_at=utcnow_iso(),
        )
        for chunk, claims in zip(outcome.chunks, outcome.claims_by_chunk, strict=True)
    ]

    novelty_threshold = get_settings().librarian_novelty_threshold
    knowledge_ids: list[int] = []
    for idea in outcome.ideas:
        proposal = idea.proposal
        knowledge_id = knowledge_repo.record(
            document_id=outcome.document_id,
            source_chunk_ids=[chunk_ids[i] for i in idea.chunk_indices],
            layer=proposal.layer,
            core_idea=proposal.core_idea,
            category=proposal.category,
            applicable_markets=proposal.applicable_markets,
            applicable_timeframes=proposal.applicable_timeframes,
            strengths=proposal.strengths,
            weaknesses=proposal.weaknesses,
            implementation_difficulty=proposal.implementation_difficulty,
            required_operators=proposal.required_operators,
            proposed_experiments=proposal.proposed_experiments,
            novelty_score=idea.novelty,
            extraction_confidence=proposal.extraction_confidence,
            extraction_prompt_version=outcome.synthesis_prompt_version,
            extracted_at=utcnow_iso(),
        )
        knowledge_ids.append(knowledge_id)

        if idea.novelty >= novelty_threshold:
            # App-Flow §3.1's "novelty push" — a standout idea skips the
            # nightly wait and reaches A1 directly. `GENERATE_SPEC.run()`
            # requires a `goal_id` (Implementation_Plan §10) — this event
            # carries no strategy/experiment to derive one from, so the
            # push targets every ACTIVE goal the idea's own
            # applicable_markets/timeframes match (any active goal, if the
            # idea named none), one job each. An idea matching no active
            # goal is still recorded as `external_knowledge` — it simply
            # waits for the next nightly batch or a future matching goal.
            for goal in _matching_active_goals(conn, proposal):
                emit(
                    conn,
                    Event.HIGH_NOVELTY_EXTRACTION,
                    payload={
                        "goal_id": goal["id"],
                        "external_knowledge_id": knowledge_id,
                        "novelty_score": idea.novelty,
                    },
                    dedupe_key=f"high_novelty_extraction:{knowledge_id}:{goal['id']}",
                )

    documents.mark_extracted(outcome.document_id, chunk_count=len(outcome.chunks))

    if outcome.origin_question_id is not None and knowledge_ids:
        # Closes the curiosity loop's first half (App-Flow §12): a
        # collector's find, extracted, answers the question that motivated
        # the search.
        ResearchQuestionRepository(conn).record_answer(outcome.origin_question_id, knowledge_ids)

    budget_consume(conn, "global", "tokens", "day", outcome.tokens_spent)
    return HandlerResult(tokens_spent=outcome.tokens_spent)


# -- COLLECT_MARKET_DATA (freshness/coverage only — see module docstring) ------


@dataclass(frozen=True)
class MarketStatsOutcome:
    stats: list[dict[str, Any]] = field(default_factory=list)


def run_market_stats(conn: sqlite3.Connection, job: Row) -> MarketStatsOutcome:
    """No LLM, no network. Reads freshness/coverage per already-ingested
    `(market, timeframe)` pair — never recomputes regime labels or
    volatility, which needs a real vendor feed this stage does not have
    (module docstring)."""
    rows = conn.execute(
        """SELECT market, timeframe, COUNT(*) AS snapshot_count,
                  MAX(period_end) AS latest_period_end, SUM(bar_count) AS total_bars
             FROM data_snapshots
            WHERE validation_status = 'valid'
            GROUP BY market, timeframe
            ORDER BY market, timeframe"""
    ).fetchall()
    return MarketStatsOutcome(stats=[dict(row) for row in rows])


def persist_market_stats(conn: sqlite3.Connection, job: Row, outcome: MarketStatsOutcome) -> HandlerResult:
    AuditLogRepository(conn).record(
        actor="system",
        action="collect_market_data.freshness",
        evidence={"markets": outcome.stats},
        reasoning=(
            "Stage 10 scope note: coverage/freshness only; regime and realised-volatility "
            "recomputation needs a configured data vendor, deferred to Stage 11."
        ),
    )
    return HandlerResult(tokens_spent=0)

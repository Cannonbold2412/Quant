"""Screen: Knowledge (UI-UX-Brief §7) — browsable memory. The two trust
tiers are never mixed in one list (§7, Backend-Schema §9/§10):
`knowledge_entries` is internal and tested, `external_knowledge` is an
untested candidate. Read-only — nothing on this screen writes.
"""
from __future__ import annotations

import sqlite3
from re import Match

from ...db.repositories import (
    DocumentChunkRepository,
    ExternalKnowledgeRepository,
    KnowledgeEdgeRepository,
    KnowledgeEntryRepository,
    ResearchQuestionRepository,
)
from ...db.repositories.base import Row
from ..charts import knowledge_graph_svg
from ..html import badge, empty_state, escape, page, table
from ..server import Response, route

__all__: list[str] = []

_LIST_LIMIT = 100


def _entries_html(conn: sqlite3.Connection) -> str:
    entries = KnowledgeEntryRepository(conn).find(superseded_by=None, order_by="id DESC", limit=_LIST_LIMIT)
    if not entries:
        return "<h2>Lessons &amp; rules</h2>" + empty_state("no lessons recorded yet")

    cards = []
    for entry in entries:
        evidence = entry.get("evidence_count") or 0
        counter = entry.get("counter_evidence_count") or 0
        # A lesson with contradicting evidence must LOOK visibly less
        # certain (§7), not just carry a number a human has to notice.
        contradiction = f' {badge(f"{counter}× contradicted", "medium")}' if counter else ""
        cards.append(
            '<div class="card">'
            f'<strong>{escape(entry.get("title") or "")}</strong> '
            f'<span class="muted">{escape(entry.get("scope") or "")} · {escape(entry.get("entry_type") or "")}</span>'
            f"{contradiction}"
            f'<p>{escape(entry.get("statement") or "")}</p>'
            f'<p class="muted">evidence {evidence} · counter-evidence {counter} · '
            f'confidence {entry.get("confidence") if entry.get("confidence") is not None else "n/a"}</p>'
            "</div>"
        )
    return "<h2>Lessons &amp; rules</h2>" + "".join(cards)


def _external_html(conn: sqlite3.Connection) -> str:
    ideas = ExternalKnowledgeRepository(conn).find(order_by="id DESC", limit=_LIST_LIMIT)
    if not ideas:
        return "<h2>External claims</h2>" + empty_state("no external ideas extracted yet")

    chunks = DocumentChunkRepository(conn)
    cards = []
    for idea in ideas:
        citations = []
        for chunk_id in idea.get("source_chunk_ids") or []:
            chunk = chunks.get(chunk_id)
            if chunk is None:
                continue
            span = f"chars {chunk.get('char_start')}-{chunk.get('char_end')}"
            citations.append(escape(f"{chunk.get('section_title') or 'untitled section'} ({span})"))
        citation_html = f'<p class="muted">source: {" · ".join(citations)}</p>' if citations else ""
        cards.append(
            '<div class="card">'
            + badge("external claim — untested", "medium")
            + f'<p>{escape(idea.get("core_idea") or "")}</p>'
            + f'<p class="muted">novelty {idea.get("novelty_score")} · extraction confidence '
            + f'{idea.get("extraction_confidence")}</p>'
            + citation_html
            + "</div>"
        )
    return "<h2>External claims</h2>" + "".join(cards)


def _graph_html(conn: sqlite3.Connection) -> str:
    edges = KnowledgeEdgeRepository(conn).find(order_by="id DESC", limit=200)
    if not edges:
        return "<h2>Knowledge graph</h2>" + empty_state("no graph edges yet")
    svg = knowledge_graph_svg(edges)
    edge_table = table(
        edges,
        ["subject", "predicate", "object", "evidence_count", "counter_evidence_count"],
        headers=["Subject", "Predicate", "Object", "Evidence", "Counter-evidence"],
    )
    return f"<h2>Knowledge graph</h2>{svg}{edge_table}"


def _questions_html(conn: sqlite3.Connection) -> str:
    questions = ResearchQuestionRepository(conn).find(order_by="priority DESC, id DESC", limit=_LIST_LIMIT)
    if not questions:
        return "<h2>Research questions</h2>" + empty_state("the curiosity queue is empty")
    rows: list[Row] = []
    for question in questions:
        row = dict(question)
        row["paid_off"] = "yes" if question.get("produced_spec_ids") else ""
        rows.append(row)
    return "<h2>Research questions</h2>" + table(
        rows,
        ["question", "status", "priority", "origin_type", "paid_off"],
        headers=["Question", "Status", "Priority", "Origin", "Paid off?"],
    )


@route("GET", r"/knowledge")
def _browse(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    body = _entries_html(conn) + _external_html(conn) + _graph_html(conn) + _questions_html(conn)
    return Response(body=page("Knowledge", body, active="Knowledge", wide=True))

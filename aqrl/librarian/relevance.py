"""The cheap relevance filter — before any LLM cost (TRD §12.2).

Term overlap over title + abstract, not an embedding call: this filter's
whole reason to exist is to reject the obviously-irrelevant survivors
*before* anything expensive touches them, so it must itself be near-free.
"""
from __future__ import annotations

import re
from collections import Counter

__all__ = ["keywords", "score"]

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Common English words that inflate overlap without carrying topic
#: signal — a short, hand-picked list rather than a stopword-corpus
#: dependency, matching this package's stdlib-only stance.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is",
        "are", "with", "by", "from", "at", "as", "this", "that", "we", "it",
        "be", "using", "based", "study", "paper", "approach", "method",
    }
)


def _terms(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall(text.lower()) if word not in _STOPWORDS and len(word) > 2}


def keywords(text: str, *, limit: int = 6) -> list[str]:
    """The `limit` most frequent non-stopword terms in `text`, ties broken
    by first appearance. Cheap search-term derivation for a
    `research_questions` row — `ResearchQuestionDraft` (`agents/session.py`)
    carries no `search_terms` field of its own, so a collector deriving
    terms from the question's own text is simpler than changing A5's
    output shape to supply them.
    """
    words = [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2]
    counts = Counter(words)
    first_seen = {word: index for index, word in enumerate(words)}
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
    return [word for word, _count in ordered[:limit]]


def score(*, title: str | None, abstract: str | None, terms: list[str]) -> float:
    """Fraction of the query `terms` found in `title`/`abstract` — `0.0` if
    either side is empty. Deliberately simple: this only needs to separate
    "plausibly about the right topic" from "clearly not," the same bar TRD
    §12.2 sets for the pre-LLM filter, not to rank precisely.
    """
    query = {t.lower() for t in terms if t.strip()}
    if not query:
        return 0.0
    document_terms = _terms(f"{title or ''} {abstract or ''}")
    if not document_terms:
        return 0.0
    hits = sum(1 for term in query if any(term in doc_term or doc_term in term for doc_term in document_terms))
    return hits / len(query)

"""`relevance.score`/`keywords` — the cheap filter that runs BEFORE any LLM
cost (TRD §12.2), and the search-term derivation `COLLECT_PAPERS` uses for
a `research_questions` row that carries no `search_terms` of its own yet.
"""
from __future__ import annotations

from aqrl.librarian.relevance import keywords, score


def test_score_is_zero_for_no_terms():
    assert score(title="anything", abstract="anything", terms=[]) == 0.0


def test_score_is_zero_when_document_is_empty():
    assert score(title=None, abstract=None, terms=["momentum"]) == 0.0


def test_score_is_positive_for_a_matching_term():
    assert score(title="Volatility-adaptive momentum strategies", abstract=None, terms=["momentum"]) > 0.0


def test_score_is_zero_for_a_wholly_unrelated_document():
    assert score(title="A survey of amphibian migration patterns", abstract=None, terms=["deflated sharpe ratio"]) == 0.0


def test_score_increases_with_more_matching_terms():
    low = score(title="Momentum strategies in equities", abstract=None, terms=["momentum", "volatility", "crypto"])
    high = score(title="Momentum and volatility in crypto markets", abstract=None, terms=["momentum", "volatility", "crypto"])
    assert high > low


def test_keywords_drops_stopwords_and_short_words():
    result = keywords("Do practitioners make momentum volatility-adaptive in the market?")
    assert "the" not in result
    assert "in" not in result
    assert "do" not in result  # too short (len <= 2)
    assert "momentum" in result


def test_keywords_respects_limit():
    text = "alpha beta gamma delta epsilon zeta eta theta"
    assert len(keywords(text, limit=3)) == 3


def test_keywords_orders_by_frequency_then_first_appearance():
    text = "rare common common common unique"
    result = keywords(text, limit=2)
    assert result[0] == "common"

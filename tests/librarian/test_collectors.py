"""`ArxivCollector`/`FeedCollector` (Stage 10 — Implementation_Plan §13):
XML parsing against real-shaped fixtures, and the relevance-before-fetch
ordering `FeedCollector` uses to bound its own network use.
"""
from __future__ import annotations

from urllib.parse import quote

from aqrl.librarian.collectors import _ARXIV_API, _ARXIV_HTML, ArxivCollector, FeedCollector
from aqrl.librarian.fetch import StubFetcher

_ARXIV_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2101.01234v1</id>
    <title>Volatility-Adaptive Momentum Strategies</title>
    <summary>We propose a volatility-adaptive momentum strategy for equities.</summary>
    <published>2021-01-05T00:00:00Z</published>
    <author><name>Jane Doe</name></author>
    <author><name>John Smith</name></author>
  </entry>
</feed>
"""

_RSS_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
  <title>Momentum works in trending markets</title>
  <link>https://blog.example.com/momentum</link>
  <description>A post about momentum.</description>
  <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
</item>
<item>
  <title>Best recipes for sourdough bread</title>
  <link>https://blog.example.com/bread</link>
  <description>Nothing about markets here.</description>
  <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
</item>
</channel></rss>
"""


def _arxiv_query_url(terms: list[str], *, max_results: int = 10) -> str:
    query = " AND ".join(f"all:{t}" for t in terms)
    return f"{_ARXIV_API}?search_query={quote(query)}&start=0&max_results={max_results}"


# -- ArxivCollector --------------------------------------------------------------


def test_arxiv_empty_terms_returns_empty_list():
    assert ArxivCollector(StubFetcher({})).search([]) == []


def test_arxiv_parses_entry_and_fetches_html_full_text():
    query_url = _arxiv_query_url(["momentum"])
    html_url = _ARXIV_HTML.format(arxiv_id="2101.01234v1")
    fetcher = StubFetcher(
        {
            query_url: _ARXIV_ATOM,
            html_url: b"<html><body><h1>Method</h1><p>Full text body here.</p></body></html>",
        }
    )
    docs = ArxivCollector(fetcher).search(["momentum"])
    assert len(docs) == 1
    doc = docs[0]
    assert doc.source == "arxiv"
    assert doc.source_id == "2101.01234v1"
    assert doc.title == "Volatility-Adaptive Momentum Strategies"
    assert doc.authors == "Jane Doe, John Smith"
    assert "Full text body here." in doc.text
    assert "# Method" in doc.text


def test_arxiv_falls_back_to_abstract_when_html_unavailable():
    query_url = _arxiv_query_url(["momentum"])
    fetcher = StubFetcher({query_url: _ARXIV_ATOM})  # no HTML URL configured
    docs = ArxivCollector(fetcher).search(["momentum"])
    assert len(docs) == 1
    assert docs[0].text == "We propose a volatility-adaptive momentum strategy for equities."


def test_arxiv_returns_empty_on_fetch_failure():
    fetcher = StubFetcher({})  # nothing configured -> the query itself fails
    assert ArxivCollector(fetcher).search(["momentum"]) == []


# -- FeedCollector -----------------------------------------------------------------


def test_feed_filters_by_relevance_before_fetching_article_bodies():
    fetcher = StubFetcher(
        {
            "https://blog.example.com/feed.xml": _RSS_FEED,
            "https://blog.example.com/momentum": b"<h1>Momentum</h1><p>Momentum works because trends persist.</p>",
        }
    )
    docs = FeedCollector(fetcher, feeds=[("blog", "https://blog.example.com/feed.xml")]).search(["momentum"])

    assert len(docs) == 1
    assert docs[0].title == "Momentum works in trending markets"
    assert "Momentum works because trends persist." in docs[0].text
    # The irrelevant "bread" item's link was never requested.
    assert "https://blog.example.com/bread" not in fetcher.requested


def test_feed_falls_back_to_summary_when_article_body_fetch_fails():
    fetcher = StubFetcher({"https://blog.example.com/feed.xml": _RSS_FEED})  # no article body configured
    docs = FeedCollector(fetcher, feeds=[("blog", "https://blog.example.com/feed.xml")]).search(["momentum"])
    assert len(docs) == 1
    assert docs[0].text == "A post about momentum."


def test_feed_returns_empty_when_the_feed_itself_is_unreachable():
    fetcher = StubFetcher({})
    docs = FeedCollector(fetcher, feeds=[("blog", "https://blog.example.com/feed.xml")]).search(["momentum"])
    assert docs == []


def test_feed_source_tag_is_carried_onto_each_doc():
    fetcher = StubFetcher(
        {
            "https://ssrn.example.com/feed.xml": _RSS_FEED,
            "https://blog.example.com/momentum": b"<p>body</p>",
        }
    )
    docs = FeedCollector(fetcher, feeds=[("ssrn", "https://ssrn.example.com/feed.xml")]).search(["momentum"])
    assert docs[0].source == "ssrn"

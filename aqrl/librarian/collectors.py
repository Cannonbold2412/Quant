"""The collectors — pure Python, no LLM (Implementation_Plan §13: *"Claude
never crawls"*). arXiv and RSS/Atom feeds (SSRN, blogs, journals) share one
`Collector` protocol and, past the API-specific query, one XML parsing path
— TRD §12.2's "single uniform pipeline for every source type" applied at
the collector layer, not just the Librarian's extraction layer.

GitHub is deliberately not implemented here (`COLLECT_GITHUB` stays
unregistered) — scoped out of Stage 10 on purpose; market data is a
separate, non-document collector (`orchestration/handlers/librarian.py`'s
`run_market_stats`).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from .fetch import FetchError, Fetcher
from .html_text import html_to_text
from .relevance import score as relevance_score

__all__ = ["ArxivCollector", "CollectedDoc", "Collector", "FeedCollector"]

_ARXIV_API = "http://export.arxiv.org/api/query"
_ARXIV_HTML = "https://arxiv.org/html/{arxiv_id}"


@dataclass(frozen=True)
class CollectedDoc:
    source: str  # 'arxiv' | 'ssrn' | 'blog' | 'journal' — external_documents.source
    source_id: str
    url: str | None
    title: str
    authors: str | None
    published_at: str | None
    text: str


class Collector(Protocol):
    def search(self, terms: list[str]) -> list[CollectedDoc]:
        """Documents matching `terms`. Network failures are swallowed and
        return fewer results, never raised — one dead feed must not stop a
        collection run that touches several sources."""
        ...


# -- shared XML plumbing (RSS <item> and Atom <entry> alike) -------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text_of(elem: ET.Element, local_name: str) -> str | None:
    for child in elem:
        if _local(child.tag) == local_name and child.text:
            text = child.text.strip()
            if text:
                return text
    return None


def _link_of(elem: ET.Element) -> str | None:
    for child in elem:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if href:
            return href
        if child.text and child.text.strip():
            return child.text.strip()
    return None


def _author_of(elem: ET.Element) -> str | None:
    for child in elem:
        if _local(child.tag) == "author":
            for grandchild in child:
                if _local(grandchild.tag) == "name" and grandchild.text:
                    return grandchild.text.strip()
            if child.text and child.text.strip():
                return child.text.strip()
        if _local(child.tag) == "creator" and child.text:
            return child.text.strip()
    return None


def _authors_of(elem: ET.Element) -> str | None:
    """arXiv Atom entries carry one `<author>` per person — `_author_of`
    only ever returns the first."""
    names = [
        grandchild.text.strip()
        for child in elem
        if _local(child.tag) == "author"
        for grandchild in child
        if _local(grandchild.tag) == "name" and grandchild.text and grandchild.text.strip()
    ]
    return ", ".join(names) or None


def _parse_feed_items(xml_bytes: bytes) -> list[dict[str, str | None]]:
    root = ET.fromstring(xml_bytes)
    items: list[dict[str, str | None]] = []
    for elem in root.iter():
        if _local(elem.tag) not in ("item", "entry"):
            continue
        items.append(
            {
                "title": _text_of(elem, "title"),
                "description": _text_of(elem, "description") or _text_of(elem, "summary"),
                "link": _link_of(elem),
                "published": _text_of(elem, "pubDate") or _text_of(elem, "published") or _text_of(elem, "updated"),
                "author": _author_of(elem),
            }
        )
    return items


class ArxivCollector:
    """The arXiv Atom API (`export.arxiv.org/api/query`) — no key required.
    Each hit is followed to `arxiv.org/html/<id>` for full text (so the
    structural chunker has real sections to split), falling back to the
    abstract for papers without an HTML rendering (older submissions)."""

    source = "arxiv"

    def __init__(self, fetcher: Fetcher, *, max_results: int = 10) -> None:
        self.fetcher = fetcher
        self.max_results = max_results

    def search(self, terms: list[str]) -> list[CollectedDoc]:
        query = " AND ".join(f"all:{term}" for term in terms if term.strip())
        if not query:
            return []
        url = f"{_ARXIV_API}?search_query={quote(query)}&start=0&max_results={self.max_results}"
        try:
            raw = self.fetcher.get(url, accept="application/atom+xml")
        except FetchError:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []

        docs: list[CollectedDoc] = []
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            entry_id = _text_of(entry, "id") or ""
            arxiv_id = entry_id.rsplit("/", 1)[-1]
            if not arxiv_id:
                continue
            title = " ".join((_text_of(entry, "title") or "").split())
            abstract = " ".join((_text_of(entry, "summary") or "").split())
            docs.append(
                CollectedDoc(
                    source=self.source,
                    source_id=arxiv_id,
                    url=f"https://arxiv.org/abs/{arxiv_id}",
                    title=title,
                    authors=_authors_of(entry),
                    published_at=_text_of(entry, "published"),
                    text=self._full_text(arxiv_id) or abstract,
                )
            )
        return docs

    def _full_text(self, arxiv_id: str) -> str | None:
        try:
            raw = self.fetcher.get(_ARXIV_HTML.format(arxiv_id=arxiv_id), accept="text/html")
        except FetchError:
            return None
        text = html_to_text(raw.decode("utf-8", errors="replace"))
        return text or None


class FeedCollector:
    """One RSS/Atom reader over configured feed URLs, each tagged with the
    `external_documents.source` value it writes — SSRN and blogs are the
    same code path with different config (module docstring). A feed item's
    title/summary is checked against `terms` before its body is fetched,
    bounding this collector's own network use to plausibly-relevant items.
    """

    def __init__(self, fetcher: Fetcher, *, feeds: list[tuple[str, str]]) -> None:
        self.fetcher = fetcher
        self.feeds = feeds  # [(source, feed_url), ...]

    def search(self, terms: list[str]) -> list[CollectedDoc]:
        docs: list[CollectedDoc] = []
        for source, feed_url in self.feeds:
            docs.extend(self._search_one(source, feed_url, terms))
        return docs

    def _search_one(self, source: str, feed_url: str, terms: list[str]) -> list[CollectedDoc]:
        try:
            raw = self.fetcher.get(feed_url, accept="application/rss+xml, application/atom+xml, text/xml")
        except FetchError:
            return []
        try:
            items = _parse_feed_items(raw)
        except ET.ParseError:
            return []

        docs: list[CollectedDoc] = []
        for item in items:
            title = item.get("title") or ""
            summary = item.get("description") or ""
            if terms and relevance_score(title=title, abstract=summary, terms=terms) <= 0.0:
                continue
            link = item.get("link")
            if not link:
                continue
            body = summary
            try:
                raw_body = self.fetcher.get(link, accept="text/html")
                body = html_to_text(raw_body.decode("utf-8", errors="replace")) or summary
            except FetchError:
                pass  # fall back to the feed's own summary
            docs.append(
                CollectedDoc(
                    source=source,
                    source_id=link,
                    url=link,
                    title=" ".join(title.split()),
                    authors=item.get("author"),
                    published_at=item.get("published"),
                    text=body,
                )
            )
        return docs

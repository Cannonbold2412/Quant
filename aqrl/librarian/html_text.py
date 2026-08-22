"""HTML -> text: trafilatura when installed, stdlib fallback always.

The one normalisation collected article/paper HTML needs before it can flow
through `chunking.chunk_by_structure`'s Markdown-heading detector:
`<h1>`-`<h3>` become `#`/`##`/`###` lines, `<p>`/block elements become
paragraph breaks, everything else (scripts, styles, nav chrome) is dropped.
Not a general-purpose HTML-to-Markdown converter — just enough fidelity that
HTML and native Markdown documents chunk through the same one path (TRD
§12.2: *"a single uniform pipeline for every source type"*).

**Two tiers.** When the optional `extraction` extra is installed
(`pip install aqrl[extraction]` → `trafilatura`), its main-content
algorithm strips boilerplate (menus, cookie banners, related-posts
chrome) the stdlib parser keeps, so blog/journal bodies arrive cleaner
and chunk boundaries fall on real section breaks more often. Without it
— or whenever it fails or returns nothing — the built-in `_TextExtractor`
below handles every input, which is why no test ever requires
trafilatura to be present.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["html_to_text"]

_HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "br", "tr", "blockquote"}
_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript"}

# trafilatura flattens <hN> elements into plain body text, which would cost
# chunk_by_structure every section boundary. So before extraction each
# heading is pre-transformed into a plain paragraph carrying a `@H<n>@`
# sentinel, and after extraction the sentinel lines are mapped back to
# ATX markers. The sentinel is deliberately ugly ASCII so it can never
# collide with real prose.
_HEADING_PRE_RE = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_HEADING_POST_RE = re.compile(r"^[ \t]*@H([1-6])@[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_TAG_RE = re.compile(r"<[^>]+>")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._heading_prefix: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _HEADING_TAGS:
            self._parts.append("\n\n")
            self._heading_prefix = _HEADING_TAGS[tag]
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _HEADING_TAGS:
            self._parts.append("\n\n")
            self._heading_prefix = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._heading_prefix is not None:
            self._parts.append(f"{self._heading_prefix} {text}")
            self._heading_prefix = None  # only the first data run in a heading gets the marker
        else:
            self._parts.append(text + " ")

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse runs of 3+ newlines down to a paragraph break; leave single
        # newlines (mid-sentence data splits) as plain spaces via handle_data's
        # own trailing space.
        lines = [line.strip() for line in joined.splitlines()]
        out: list[str] = []
        blank_run = 0
        for line in lines:
            if not line:
                blank_run += 1
                continue
            if out and blank_run:
                out.append("")
            blank_run = 0
            out.append(line)
        return "\n".join(out).strip()


def _trafilatura_extract(html: str) -> str | None:
    """Main-content extraction via the optional `extraction` extra.
    Returns `None` whenever trafilatura is not installed, raises, or
    finds no main content — every such case falls through to the
    stdlib parser, which handles all inputs."""
    try:
        import trafilatura
    except ImportError:
        return None

    def _heading_to_sentinel(match: re.Match[str]) -> str:
        inner = " ".join(_TAG_RE.sub(" ", match.group(2)).split())
        return f"<p>@H{int(match.group(1))}@ {inner}</p>"

    try:
        prepared = _HEADING_PRE_RE.sub(_heading_to_sentinel, html)
        text = trafilatura.extract(
            prepared,
            output_format="markdown",
            include_formatting=True,
            include_tables=True,
            include_images=False,
            include_links=False,
        )
    except Exception:  # noqa: BLE001 - extraction must never break collection
        return None
    if not text:
        return None

    def _sentinel_to_heading(match: re.Match[str]) -> str:
        level = min(int(match.group(1)), 4)
        return f"{'#' * level} {match.group(2)}"

    restored = _HEADING_POST_RE.sub(_sentinel_to_heading, text)
    return restored or None


def html_to_text(html: str) -> str:
    """Render `html` down to Markdown-flavoured plain text — headings
    prefixed with `#`, paragraphs separated by a blank line — so it chunks
    through `chunking.chunk_by_structure` identically to a native Markdown
    document. Uses trafilatura when available (boilerplate stripped),
    falling back to the stdlib parser for everything else."""
    extracted = _trafilatura_extract(html)
    if extracted is not None:
        return _normalize_markdown(extracted)
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def _normalize_markdown(text: str) -> str:
    """Tidy trafilatura's markdown into exactly the shape `html_to_text`
    promises: ATX heading lines, single blank line between blocks, no
    stray link/image syntax, no trailing whitespace per line."""
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            continue
        if out and blank_run:
            out.append("")
        blank_run = 0
        out.append(line)
    return "\n".join(out).strip()

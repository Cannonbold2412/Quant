"""HTML -> text, stdlib only (`html.parser`).

The one normalisation collected article/paper HTML needs before it can flow
through `chunking.chunk_by_structure`'s Markdown-heading detector:
`<h1>`-`<h3>` become `#`/`##`/`###` lines, `<p>`/block elements become
paragraph breaks, everything else (scripts, styles, nav chrome) is dropped.
Not a general-purpose HTML-to-Markdown converter — just enough fidelity that
HTML and native Markdown documents chunk through the same one path (TRD
§12.2: *"a single uniform pipeline for every source type"*).
"""
from __future__ import annotations

from html.parser import HTMLParser

__all__ = ["html_to_text"]

_HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "br", "tr", "blockquote"}
_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript"}


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


def html_to_text(html: str) -> str:
    """Render `html` down to Markdown-flavoured plain text — headings
    prefixed with `#`, paragraphs separated by a blank line — so it chunks
    through `chunking.chunk_by_structure` identically to a native Markdown
    document."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()

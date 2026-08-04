"""Chunk a document by structure, never a blind token window (TRD §12.2:
*"a blind token window risks cutting an equation across a boundary"*).

Pure and I/O-free — the only file in this package worth fuzzing hard, since
every downstream extraction quality claim depends on chunk boundaries
actually falling on section breaks rather than mid-thought.

**Two splitting rules, applied in order.** First, split on detected
headings (Markdown ATX `#`/`##`, Markdown setext `===`/`---` underlines, and
numbered section titles like `2.1 Method`) — a document with real structure
never reaches the second rule. Second, a **paragraph-boundary fallback**
for any section too large on its own (no sub-headings, or none detected at
all): accumulate whole paragraphs up to `max_chars`, cutting only at a blank
line, never inside one — the property that keeps an equation or a table
intact even when nothing in the source names a section.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Chunk", "chunk_by_structure"]

#: A chunk above this size (chars, not tokens — no tokenizer dependency for
#: what is ultimately a heuristic cut point) triggers the paragraph fallback.
_DEFAULT_MAX_CHARS = 4000

_ATX_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_SETEXT_RE = re.compile(r"^([^\n]{1,200})\n(?:=+|-{3,})[ \t]*$", re.MULTILINE)
#: "1. Introduction", "2.1 Method", "3 Results" on their own line — a title
#: fragment, not a numbered list item mid-sentence (hence the capital-letter
#: start and the length cap).
_NUMBERED_RE = re.compile(r"^\d+(?:\.\d+)*\.?[ \t]+([A-Z][A-Za-z0-9 ,'()\-]{2,78})[ \t]*$", re.MULTILINE)
_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n")


@dataclass(frozen=True)
class Chunk:
    index: int
    section_title: str | None
    char_start: int
    char_end: int
    text: str


def _heading_spans(text: str) -> list[tuple[int, int, str]]:
    """`(start, end, title)` for every detected heading, earliest first.
    Overlapping matches from different patterns keep whichever starts
    first — a setext title line matched inside an already-accepted ATX
    heading's span, for instance, is dropped rather than double-counted."""
    candidates: list[tuple[int, int, str]] = []
    for pattern, group in ((_ATX_RE, 1), (_SETEXT_RE, 1), (_NUMBERED_RE, 1)):
        for match in pattern.finditer(text):
            candidates.append((match.start(), match.end(), match.group(group).strip()))
    candidates.sort(key=lambda c: c[0])

    accepted: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, title in candidates:
        if start < last_end:
            continue
        accepted.append((start, end, title))
        last_end = end
    return accepted


def _split_oversized(body: str, max_chars: int) -> list[tuple[int, int]]:
    """Greedy paragraph accumulation: grow a span until the next paragraph
    would push it past `max_chars`, then cut — always at a blank line,
    never mid-paragraph. A body with no paragraph breaks at all (one huge
    block) returns as a single span; nothing else can be done to it without
    the blind mid-token cut this module exists to avoid."""
    boundaries = sorted({0, *(m.end() for m in _PARAGRAPH_BREAK_RE.finditer(body)), len(body)})
    spans: list[tuple[int, int]] = []
    seg_start = boundaries[0]
    prev = boundaries[0]
    for boundary in boundaries[1:]:
        if boundary - seg_start > max_chars and prev > seg_start:
            spans.append((seg_start, prev))
            seg_start = prev
        prev = boundary
    spans.append((seg_start, boundaries[-1]))
    return spans


def chunk_by_structure(text: str, *, max_chars: int = _DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Split `text` into chunks along structural boundaries.

    A document under `max_chars` with no detected headings comes back as a
    single chunk (`1` for short documents, Backend-Schema §10's
    `external_documents.chunk_count`). Empty/whitespace-only input returns
    `[]` rather than a chunk of nothing.
    """
    if not text or not text.strip():
        return []

    headings = _heading_spans(text)
    sections: list[tuple[str | None, int, int]] = []
    if not headings:
        sections.append((None, 0, len(text)))
    else:
        first_start = headings[0][0]
        if first_start > 0 and text[:first_start].strip():
            sections.append((None, 0, first_start))
        for position, (start, _end, title) in enumerate(headings):
            next_start = headings[position + 1][0] if position + 1 < len(headings) else len(text)
            sections.append((title, start, next_start))

    chunks: list[Chunk] = []
    index = 0
    for title, start, end in sections:
        body = text[start:end]
        if len(body) <= max_chars:
            chunks.append(Chunk(index=index, section_title=title, char_start=start, char_end=end, text=body))
            index += 1
            continue
        for sub_start, sub_end in _split_oversized(body, max_chars):
            chunks.append(
                Chunk(
                    index=index,
                    section_title=title,
                    char_start=start + sub_start,
                    char_end=start + sub_end,
                    text=body[sub_start:sub_end],
                )
            )
            index += 1
    return chunks

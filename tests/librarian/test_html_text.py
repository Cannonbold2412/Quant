"""`html_to_text` — HTML -> the same Markdown-flavoured text `chunking.
chunk_by_structure` already knows how to split, so HTML and native
Markdown documents flow through one pipeline (TRD §12.2)."""
from __future__ import annotations

from aqrl.librarian.chunking import chunk_by_structure
from aqrl.librarian.html_text import html_to_text


def test_headings_become_markdown_atx():
    html = "<html><body><h1>Title</h1><p>Intro text.</p><h2>Method</h2><p>Body text.</p></body></html>"
    text = html_to_text(html)
    assert "# Title" in text
    assert "## Method" in text


def test_scripts_and_styles_are_dropped():
    html = "<html><body><script>evil()</script><style>.x{}</style><p>Real content.</p></body></html>"
    text = html_to_text(html)
    assert "evil" not in text
    assert "Real content." in text


def test_output_chunks_by_structure_like_a_native_markdown_document():
    html = "<h1>Intro</h1><p>Some text.</p><h2>Method</h2><p>More text.</p>"
    text = html_to_text(html)
    chunks = chunk_by_structure(text)
    assert [c.section_title for c in chunks] == ["Intro", "Method"]


def test_empty_html_returns_empty_text():
    assert html_to_text("") == ""
    assert html_to_text("<html><body></body></html>") == ""

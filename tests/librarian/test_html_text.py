"""`html_to_text` — HTML -> the same Markdown-flavoured text `chunking.
chunk_by_structure` already knows how to split, so HTML and native
Markdown documents flow through one pipeline (TRD §12.2).

The first four tests must pass on the stdlib fallback alone (trafilatura
is an optional extra). The `pytest.importorskip` block exercises the
trafilatura tier and only runs when the `extraction` extra is installed."""
from __future__ import annotations

import pytest

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


# -- trafilatura tier (optional `extraction` extra) -----------------------------


def test_trafilatura_strips_boilerplate_but_keeps_headings():
    pytest.importorskip("trafilatura")
    html = (
        "<html><body>"
        "<nav><a href='/'>Home</a> <a href='/about'>About</a></nav>"
        "<h1>Intro</h1><p>Some text about momentum strategies.</p>"
        "<h2>Method</h2><p>We compute realized volatility from tick data.</p>"
        "<p>Additional substantive paragraph so main-content detection has "
        "enough body text to work with reliably.</p>"
        "</body></html>"
    )
    text = html_to_text(html)
    assert "# Intro" in text
    assert "## Method" in text
    assert "Home" not in text
    assert "About" not in text


def test_trafilatura_output_chunks_by_structure():
    pytest.importorskip("trafilatura")
    html = (
        "<html><body>"
        "<h1>Intro</h1><p>Opening paragraph with enough words to register.</p>"
        "<h2>Method</h2><p>Method paragraph, likewise long enough to count.</p>"
        "<h3>Data</h3><p>Data paragraph describing the one-minute bar feed.</p>"
        "</body></html>"
    )
    chunks = chunk_by_structure(html_to_text(html))
    assert [c.section_title for c in chunks] == ["Intro", "Method", "Data"]


def test_trafilatura_degenerate_inputs_fall_back_cleanly():
    pytest.importorskip("trafilatura")
    assert html_to_text("") == ""
    assert html_to_text("   \n\t  ") == ""
    assert html_to_text("<html><body></body></html>") == ""

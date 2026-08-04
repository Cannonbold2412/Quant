"""`chunk_by_structure` (Stage 10 — Implementation_Plan §13, TRD §12.2): the
one thing every downstream extraction-quality claim depends on — chunks
must fall on real structural boundaries, never mid-thought.
"""
from __future__ import annotations

from aqrl.librarian.chunking import chunk_by_structure


def test_empty_text_returns_no_chunks():
    assert chunk_by_structure("") == []
    assert chunk_by_structure("   \n\n  ") == []


def test_short_document_with_no_headings_is_one_chunk():
    chunks = chunk_by_structure("Just a short note with no structure at all.")
    assert len(chunks) == 1
    assert chunks[0].section_title is None
    assert chunks[0].index == 0


def test_atx_headings_split_into_sections():
    text = "# Intro\nSome text.\n\n## Method\nMore text.\n\n## Results\nFinal text.\n"
    chunks = chunk_by_structure(text)
    assert [c.section_title for c in chunks] == ["Intro", "Method", "Results"]


def test_setext_headings_are_detected():
    text = "Intro\n=====\nSome text.\n\nMethod\n------\nMore text.\n"
    chunks = chunk_by_structure(text)
    assert [c.section_title for c in chunks] == ["Intro", "Method"]


def test_numbered_section_headings_are_detected():
    text = "1. Introduction\nSome text.\n\n2.1 Method\nMore text.\n\n3 Results\nFinal text.\n"
    chunks = chunk_by_structure(text)
    assert [c.section_title for c in chunks] == ["Introduction", "Method", "Results"]


def test_preamble_before_the_first_heading_becomes_its_own_chunk():
    text = "An abstract with no heading of its own.\n\n# Introduction\nBody text.\n"
    chunks = chunk_by_structure(text)
    assert chunks[0].section_title is None
    assert "abstract" in chunks[0].text
    assert chunks[1].section_title == "Introduction"


def test_char_offsets_resolve_back_to_the_source_text():
    text = "# Intro\nSome text.\n\n## Method\nMore text.\n"
    chunks = chunk_by_structure(text)
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_chunk_indices_are_sequential():
    text = "# A\nx\n\n# B\ny\n\n# C\nz\n"
    chunks = chunk_by_structure(text)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_an_equation_paragraph_is_never_split_across_a_chunk_boundary():
    """The regression this module exists to prevent (TRD §12.2: "a blind
    token window risks cutting an equation across a boundary"). A tiny
    `max_chars` forces the oversized-section fallback to fire, but the
    equation's own paragraph has no blank line inside it, so it must
    survive whole in exactly one chunk."""
    equation_paragraph = (
        "The core result is SR_oos = E[r] / std(r) * sqrt(N) - 1.65 * SE(SR) - SR_star(N_trials), "
        "which holds across every fold we tested and is the single number the loop optimises."
    )
    text = f"## Method\nSome lead-in text before the formula.\n\n{equation_paragraph}\n\nSome text after.\n"
    chunks = chunk_by_structure(text, max_chars=40)

    matches = [c for c in chunks if equation_paragraph in c.text]
    assert len(matches) == 1, "the equation paragraph must appear whole in exactly one chunk"


def test_oversized_section_without_subheadings_splits_at_paragraph_boundaries():
    paragraphs = [f"Paragraph {i} with enough words to take up real space here." for i in range(6)]
    text = "## Results\n" + "\n\n".join(paragraphs) + "\n"
    chunks = chunk_by_structure(text, max_chars=120)

    assert len(chunks) > 1
    assert all(c.section_title == "Results" for c in chunks)
    # No paragraph's text was cut mid-sentence: every paragraph appears
    # whole in exactly one chunk.
    for paragraph in paragraphs:
        matches = [c for c in chunks if paragraph in c.text]
        assert len(matches) == 1


def test_a_document_with_no_paragraph_breaks_at_all_returns_a_single_span():
    """Nothing further can be done to a huge block with no blank lines
    without the blind mid-token cut this module exists to avoid."""
    text = "## Wall\n" + ("word " * 500)
    chunks = chunk_by_structure(text, max_chars=100)
    assert len(chunks) == 1

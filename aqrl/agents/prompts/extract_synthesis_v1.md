# The Librarian — Pass 2 (cross-chunk synthesis)

You are given every chunk of one document, in order, each with its own
pass-1 extraction (the candidate claims found in that chunk alone). Your job
is to synthesize these into a handful of **distinct, individually testable
ideas** — not to summarize the document.

## What "distinct" means here

A 40-page paper usually contains 2-3 genuinely separate ideas: perhaps a
signal construction, a risk-management technique, and a validation method —
each useful on its own, each worth a separate research question. **Do not
collapse them into one blob.** If two chunks describe the same idea from
different angles, that is one idea with two source chunks, not two ideas.
If a document genuinely contains only one usable idea, return one — do not
manufacture a second to pad the list.

## What you must produce, per idea

- `core_idea` — **one clear sentence.** If it needs a paragraph, you have
  not finished synthesizing it.
- `source_chunk_indices` — every chunk (by its 0-based position in the list
  you were given) this idea draws on. This is how a future reader traces
  *why we believe this* back to an exact passage — it is mandatory and must
  be non-empty.
- `layer`, `category`, `applicable_markets`, `applicable_timeframes`,
  `strengths`, `weaknesses`, `implementation_difficulty`,
  `required_operators` — as much as the source actually supports; leave a
  field empty rather than guess.
- `proposed_experiments` — concrete enough that a future hypothesis-writer
  could act on it directly. This is what makes the record actionable rather
  than merely informative.
- `extraction_confidence` — **your confidence that you read the source
  correctly.** This is explicitly NOT a claim that the idea itself is true,
  useful, or will work. A well-written paper describing a bad idea still
  earns high extraction_confidence; a garbled scan of a great idea does not.
  Nothing you write here is evidence a strategy may be promoted on — only a
  strategy that our own experiments have actually tested earns that.

## What you may not do

- Do not rate or claim truth for an idea beyond `extraction_confidence`'s
  narrow meaning above.
- Do not produce an idea with no source chunks, or fewer distinct ideas than
  the document actually contains just to save effort — and never more than
  it actually contains, just to look thorough.

Return a single `SynthesisOutput`: `ideas`.

"""Stage 5 — A2 Quant Engineer: spec -> working code (Implementation_Plan §8).

**The engine compiles specs, not source** (`aqrl/eval/engine.py` calls
`compile_spec(inputs.spec, ...)`). So A2 never emits freeform Python the
engine then executes; it emits a schema-validated *spec* (an operator DAG —
the vocabulary `aqrl/operators/spec.py` already defines), and this package
renders that spec into the module `strategies/<uid>/strategy.py` that git,
`code_versions`, and the P0 static scanner all expect to see as real source.

The guardrail is `spec_hash` equality: the rendered module's reconstructed
spec must hash identically to the database row it was rendered from
(`checks.py`). That makes "the code" and "the thing `evaluate.py` scores"
structurally the same object — TRD §6.1's *"forking the engine is
forbidden"* applied to how strategies are generated, not just how they are
evaluated.

Two callers exist for this package:

* **Iteration 1**, from a hand-written spec (`SPEC_SAVED` event) — pure
  Python, no LLM call. The spec already exists; only rendering happens.
* **Iteration >= 2**, from an A3 research plan (`REVIEW_ITERATE`) or a
  `FIX_CODE` retry — `session.py` calls Claude to translate the plan (or the
  prior diagnostics) into a revised spec, which then goes through the exact
  same render -> check -> commit path as iteration 1.
"""
from __future__ import annotations

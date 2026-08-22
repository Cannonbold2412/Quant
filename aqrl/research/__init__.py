"""Stage 0 — the research loop, migrated out of the former `nanoaqrl/` package.

The single-instrument loop itself: `strategy.py` (the one file meant to be
edited), `data.py` (read-only snapshot access behind the vault), `evaluate.py`
(the scoring harness and the hard bar), and the backtest / walk-forward /
cost-model / vault / persistence adapters under this package that feed them.
The shared mathematics and schema live in `aqrl.eval` and `aqrl.db`; nothing
here forks them.

Usage:
    python -m aqrl.research.evaluate score <commit>
    python -m aqrl.research.evaluate null-world --generator {permuted,block_bootstrap,synthetic_path}
"""

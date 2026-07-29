"""Stage 0's import site for the honest score. **The maths lives elsewhere.**

TRD §6.1: the statistical layer *"is market-agnostic mathematics and must exist
exactly once"* — and the reason is scientific validity, not code hygiene. Stage 3
moved the implementation into `aqrl/eval/stats/honest_score.py`; this module
re-exports it so nanoAQRL's loop and its tests keep running against the one
copy rather than a fork that drifts.
"""
from __future__ import annotations

from aqrl.eval.stats.honest_score import (
    EULER_MASCHERONI,
    HonestScoreResult,
    compute_honest_score,
    expected_max_sharpe_under_null,
    se_sharpe,
    sharpe_ratio,
)

__all__ = [
    "EULER_MASCHERONI",
    "HonestScoreResult",
    "compute_honest_score",
    "expected_max_sharpe_under_null",
    "se_sharpe",
    "sharpe_ratio",
]

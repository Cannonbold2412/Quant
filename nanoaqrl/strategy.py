"""strategy.py — signal logic, entries, exits, filters, sizing.

**This is the only file meant to be edited** (by a human today; by A2 once
Stage 5 exists). Read `program.md` before changing anything here — it
carries the anti-look-ahead rules that `evaluate.py`'s P0 checks enforce.

Example strategy: a dual moving-average crossover, filtered by a minimum
percentage gap (never an absolute price threshold — the series is meant to
be back-adjusted eventually, and an absolute level does not transfer across
instruments anyway). This is a starting point to prove the loop runs end to
end, not a claim that it clears the bar.
"""
from __future__ import annotations

import pandas as pd

PARAMS = {
    "fast_window": 20,
    "slow_window": 100,
    "min_gap_pct": 0.002,
}

# Optional coarse grid. evaluate.py tunes this inside each walk-forward
# fold's training window only (program.md, "Fitting") — never on the test
# window, so it never inflates N_trials (TRD §8.6). Set to None to skip
# tuning entirely.
PARAM_GRID = {
    "fast_window": [10, 20, 30],
    "slow_window": [60, 100, 150],
}


def generate_signals(df: pd.DataFrame, params: dict) -> pd.Series:
    """Return a raw signal in [-1, 1] per bar, computed only from data at or
    before that bar. evaluate.py applies the entry lag centrally — do not
    shift the signal yourself."""
    fast = df["close"].rolling(params["fast_window"]).mean()
    slow = df["close"].rolling(params["slow_window"]).mean()
    gap_pct = (fast - slow) / slow

    signal = pd.Series(0.0, index=df.index)
    signal[gap_pct > params["min_gap_pct"]] = 1.0
    signal[gap_pct < -params["min_gap_pct"]] = -1.0
    return signal

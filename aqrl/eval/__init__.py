"""`evaluate.py`, productionised — the one evaluation engine (TRD §6).

**One engine. Not one per market, not one per timeframe.** The statistical layer
— deflated Sharpe, White's Reality Check, CSCV/PBO, Monte Carlo, walk-forward —
is market-agnostic mathematics and exists exactly once. The reason is not code
hygiene, it is scientific validity: the Research Memory depends on comparing
experiment #6,201 in crypto against #12,483 in Indian equities, and that
comparison is only meaningful if both were scored by identical code (TRD §6.1).

What genuinely differs by market lives in **profiles**, not in branches:

    engine.py                 phases, statistics, the hard bar
      ├── MarketProfile       calendar, constraints, universe, benchmark
      ├── CostModel           keyed on (market, asset_class)
      ├── TimeframeProfile    annualisation, fill model, cost sweep, WF windows
      ├── data/validators/    per-market data sanity checks
      └── gates/              optional extra phases per market

Six markets × six asset classes × many timeframes are **profile files**.
"""

from .backtest import BacktestResult, run_backtest
from .panel import PanelError, PricePanel
from .version import EVAL_ENGINE_VERSION, engine_version

__all__ = [
    "BacktestResult",
    "EVAL_ENGINE_VERSION",
    "PanelError",
    "PricePanel",
    "engine_version",
    "run_backtest",
]

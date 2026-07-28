"""AQRL — the Stage 1 foundations (Implementation_Plan §3).

The substrate everything later assumes: configuration, content hashing,
structured logging, a migrated SQLite schema behind a thin repository layer,
content-hashed market/timeframe profiles, and a data layer whose defining
property is that **raw prices are immutable and every adjustment happens at
load time** (TRD §14.2a).

Stage 0 (`nanoaqrl/`) is the research loop itself and predates this package;
it now persists through `aqrl.db` rather than its own schema.
"""

__version__ = "0.1.0"

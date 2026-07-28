"""Persistence — schema, migrations, and a thin repository layer.

SQLite holds metadata, metrics, provenance, state and trial counts; git holds
the strategy code; `experiments.code_commit` links them (TRD §5.1). Strategy
code is never stored as a blob here — that would lose diffs, blame, and the
ability to check out and re-run a past experiment.
"""

from .connection import connect, transaction
from .migrate import migrate, status

__all__ = ["connect", "migrate", "status", "transaction"]

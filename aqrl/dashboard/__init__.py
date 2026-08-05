"""Stage 12 — Dashboard (decision layer), Implementation_Plan §15.

Built to `docs/UI-UX-Brief.md`. **Distinct from Stage 4a's Observability**
(activity feed + data explorer, `aqrl/orchestration` job-queue debugging) —
this is the decision layer only: a read-only view plus the write paths the
brief explicitly names (the two human gates, kill/retire, data-flag
resolution, and DEFER). Every write routes through the functions Stages 9
and 11 already proved (`aqrl.gates`, `aqrl.monitoring`) — nothing here is a
second implementation of a decision `aqrl review`/`aqrl deploy` already make.
"""
from __future__ import annotations

from .server import serve

__all__ = ["serve"]

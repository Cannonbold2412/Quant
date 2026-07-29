"""Back-pressure over the `budgets` table (TRD §4.4).

A budget row is created only when a human configures one (`aqrl budgets
set`). No row for a given `(scope, scope_id, budget_type, period)` means
**unlimited**, not zero — Stage 4 does not invent caps nobody asked for.
When a row does exist, it is the scheduler's job to check it *before*
dispatch, never after: `dispatch.py` calls `check_global()` once per tick
before it claims anything, so an exhausted budget shows up as an idle cause
(TRD §4.5), not as wasted work that gets thrown away.

Only the four global, day-scoped caps are gated in Stage 4's dispatch loop —
`tokens`, `experiments`, `compute_seconds`, and the concurrency cap (which
lives in `Settings`, not this table, since it is not a *quantity spent* but a
*simultaneous-workers* limit). Per-strategy and per-goal budgets are tracked
here and queryable, but nothing dispatches on them yet: Stage 4 ships only
the `EVALUATE` handler, which spends no LLM tokens, so a strategy-level token
cap has no real traffic to gate until Stage 5 adds agent calls. Gating those
at claim time (rather than before an agent session starts) would also risk an
infinite reclaim loop on a single blocked strategy while others sit idle —
better decided once there is a caller for it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..db.repositories.base import Repository, Row

__all__ = ["BackPressure", "BudgetRepository", "check", "check_global", "consume"]

_SCOPES = frozenset({"global", "strategy", "goal"})
_BUDGET_TYPES = frozenset({"tokens", "experiments", "iterations", "compute_seconds", "usd"})
_PERIODS = frozenset({"day", "week", "lifetime"})

#: The caps `dispatch.py` gates on before every claim, in this order —
#: cheapest/most-likely-exhausted first, so a blocked tick short-circuits.
GLOBAL_DISPATCH_CAPS: tuple[tuple[str, str], ...] = (
    ("experiments", "day"),
    ("tokens", "day"),
    ("compute_seconds", "day"),
)


@dataclass(frozen=True)
class BackPressure:
    allowed: bool
    reason: str | None = None


class BudgetRepository(Repository):
    table = "budgets"
    updated_column = "updated_at"

    def for_scope(self, scope: str, budget_type: str, period: str, scope_id: int | None = None) -> Row | None:
        rows = self.find(scope=scope, scope_id=scope_id, budget_type=budget_type, period=period)
        return rows[0] if rows else None

    def upsert(
        self, scope: str, budget_type: str, period: str, limit_value: int, *, scope_id: int | None = None
    ) -> int:
        """Create or re-limit a budget. Used only by the CLI / a human."""
        if scope not in _SCOPES:
            raise ValueError(f"unknown budget scope {scope!r}")
        if budget_type not in _BUDGET_TYPES:
            raise ValueError(f"unknown budget_type {budget_type!r}")
        if period not in _PERIODS:
            raise ValueError(f"unknown period {period!r}")

        existing = self.for_scope(scope, budget_type, period, scope_id)
        if existing is not None:
            self.update(existing["id"], limit_value=limit_value)
            return int(existing["id"])
        return self.insert(
            scope=scope,
            scope_id=scope_id,
            budget_type=budget_type,
            period=period,
            limit_value=limit_value,
            used_value=0,
            period_start=_period_start(period).isoformat(),
            exhausted=False,
        )


def _period_start(period: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    if period == "lifetime":
        return datetime.fromtimestamp(0, tz=UTC)
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_of_day - timedelta(days=start_of_day.weekday())
    raise ValueError(f"unknown period {period!r}")


def _roll_if_expired(conn: sqlite3.Connection, row: Row) -> Row:
    """Reset a budget's counter once its period has rolled over."""
    if row["period"] == "lifetime":
        return row
    budgets = BudgetRepository(conn)
    boundary = _period_start(row["period"]).isoformat()
    if row["period_start"] is None or row["period_start"] < boundary:
        budgets.update(row["id"], used_value=0, exhausted=False, period_start=boundary)
        return budgets.get(row["id"])
    return row


def check(conn: sqlite3.Connection, scope: str, budget_type: str, period: str, *, scope_id: int | None = None) -> BackPressure:
    """Is there room under `(scope, budget_type, period)`? Unconfigured = yes."""
    budgets = BudgetRepository(conn)
    row = budgets.for_scope(scope, budget_type, period, scope_id)
    if row is None:
        return BackPressure(True)

    row = _roll_if_expired(conn, row)
    if row["exhausted"] or int(row["used_value"]) >= int(row["limit_value"]):
        label = f"{scope}:{budget_type}:{period}" + (f"#{scope_id}" if scope_id is not None else "")
        return BackPressure(False, reason=f"budget_exhausted:{label}")
    return BackPressure(True)


def check_global(conn: sqlite3.Connection) -> BackPressure:
    """The caps `dispatch.py` gates dispatch on — see `GLOBAL_DISPATCH_CAPS`."""
    for budget_type, period in GLOBAL_DISPATCH_CAPS:
        result = check(conn, "global", budget_type, period)
        if not result.allowed:
            return result
    return BackPressure(True)


def consume(
    conn: sqlite3.Connection, scope: str, budget_type: str, period: str, amount: int, *, scope_id: int | None = None
) -> None:
    """Record spend against a budget, if one is configured. No-op otherwise —
    Stage 4 does not create a row just to track usage nobody capped."""
    if amount <= 0:
        return
    budgets = BudgetRepository(conn)
    row = budgets.for_scope(scope, budget_type, period, scope_id)
    if row is None:
        return
    row = _roll_if_expired(conn, row)
    used = int(row["used_value"]) + amount
    budgets.update(row["id"], used_value=used, exhausted=used >= int(row["limit_value"]))

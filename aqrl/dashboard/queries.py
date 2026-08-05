"""Small read-only helpers shared by more than one screen. Kept separate
from `series.py` (return-series recomputation only, the expensive path) and
from any single view module, so Decisions' empty-state and Health's own list
rank deployments by concern the same way rather than drifting apart."""
from __future__ import annotations

import sqlite3

from ..db.repositories import DeploymentRepository
from ..db.repositories.base import Row

__all__ = ["HEALTH_RANK", "deployments_by_concern", "worst_deployment"]

#: red is the most concerning, green the least — UI-UX-Brief §4.1's ranking.
#: A deployment with no health check yet (`current_health IS NULL`) ranks
#: last, same as green: nothing has flagged it as a concern.
HEALTH_RANK: dict[str | None, int] = {"red": 0, "orange": 1, "yellow": 2, "green": 3, None: 4}


def deployments_by_concern(conn: sqlite3.Connection) -> list[Row]:
    """Every deployment, flat, ranked red -> orange -> yellow -> green
    (UI-UX-Brief §4.1) — deliberately ignoring Pipeline's strategy/market
    grouping (§4.1: "burying it in a tree is how you miss the one thing you
    needed to see")."""
    deployments = DeploymentRepository(conn).find(order_by="id")
    return sorted(deployments, key=lambda row: HEALTH_RANK.get(row.get("current_health"), 4))


def worst_deployment(conn: sqlite3.Connection) -> Row | None:
    """The single most concerning deployment — what Decisions' empty state
    shows instead of a blank page (§2: "empty state is a feature, not a
    gap")."""
    ranked = deployments_by_concern(conn)
    return ranked[0] if ranked else None

"""Screen: Pipeline (UI-UX-Brief §5) — the lifecycle view: everything, and
where it sits. Grouping (strategy vs market) is a query-param toggle over
one query, never a fixed hierarchy (§5.2) — neither view is "correct", they
answer different questions.

The data-quality queue (§5.3) is "the one recurring human task besides the
two gates" — its write path is the same transaction shape `cli.py`'s
`cmd_flags_resolve` already uses (`ValidationFlagRepository.resolve` +
`SnapshotManager.revalidate`), not a second implementation.
"""
from __future__ import annotations

import sqlite3
from re import Match

from ...db import transaction
from ...db.repositories import StrategyRepository, ValidationFlagRepository
from ...db.repositories.base import Row
from ...db.repositories.promotion import DeploymentRepository
from ..html import badge, empty_state, escape, page
from ..server import Response, redirect, route

__all__: list[str] = []

#: `strategies.status` (Backend-Schema §14.1) collapsed into the six-stage
#: track UI-UX-Brief §5.1 draws. `rejected`/`quarantined` are deliberately
#: excluded — they get their own sections (§5.1: "Rejected strategies show
#: structured reasons. Quarantined strategies need human debugging").
_STAGE_BUCKETS: dict[str, str] = {
    "draft": "Research", "spec_ready": "Research", "coding": "Research", "evaluating": "Research",
    "evaluated": "Research", "iterating": "Research", "plateaued": "Research",
    "pending_promotion": "Awaiting review", "awaiting_human_review": "Awaiting review",
    "paper_trading": "Paper", "pending_live_review": "Awaiting review",
    "live_small": "Live 1-5%", "live_scaled": "Live scaled", "retired": "Retired",
}
_STAGE_ORDER = ["Research", "Awaiting review", "Paper", "Live 1-5%", "Live scaled", "Retired"]

_VALID_RESOLUTIONS = frozenset({"genuine_move", "missing_action_added", "data_error"})


def _family_market_count(conn: sqlite3.Connection, family: str) -> int:
    row = conn.execute("SELECT COUNT(DISTINCT market) AS n FROM strategies WHERE family = ?", (family,)).fetchone()
    return int(row["n"])


def _latest_deployment(conn: sqlite3.Connection, strategy_id: int) -> Row | None:
    rows = DeploymentRepository(conn).find(strategy_id=strategy_id, order_by="id DESC", limit=1)
    return rows[0] if rows else None


def _strategy_row_html(conn: sqlite3.Connection, strategy: Row, *, show_market: bool) -> str:
    status = strategy.get("status") or ""
    bucket = _STAGE_BUCKETS.get(status, status)
    deployment = _latest_deployment(conn, strategy["id"])
    health = ""
    if deployment is not None:
        level = deployment.get("current_health") or "green"
        mode = (deployment.get("mode") or "").upper()
        allocation = deployment.get("allocation_pct")
        mode_text = f"{mode} {allocation:g}%" if allocation is not None else mode
        health = f'{badge(level, level)} <span class="muted">{escape(mode_text)}</span>'
    label = strategy.get("market") if show_market else strategy.get("name")
    return (
        f'<li><a href="/health/{escape(deployment["uid"])}">{escape(label or "")}</a>'
        if deployment is not None
        else f"<li>{escape(label or '')}"
    ) + f' <span class="muted">{escape(bucket)}</span> {health}</li>'


def _grouped_by_strategy(conn: sqlite3.Connection, strategies: list[Row]) -> str:
    families: dict[str, list[Row]] = {}
    for strategy in strategies:
        families.setdefault(strategy.get("family") or "", []).append(strategy)

    sections = []
    for family, members in sorted(families.items()):
        trial_count = StrategyRepository(conn).family_trial_count(family)
        market_count = _family_market_count(conn, family)
        # UI-UX-Brief §5.4: the same idea tried across N markets is N trials
        # of one family, not N independent results — keep that reading
        # visible at the exact moment a human scans this list.
        header = f"{escape(family)} ({trial_count} family trials across {market_count} market(s))"
        rows = "".join(_strategy_row_html(conn, s, show_market=True) for s in members)
        sections.append(f"<h3>{header}</h3><ul>{rows}</ul>")
    return "".join(sections)


def _grouped_by_market(conn: sqlite3.Connection, strategies: list[Row]) -> str:
    markets: dict[str, list[Row]] = {}
    for strategy in strategies:
        markets.setdefault(strategy.get("market") or "", []).append(strategy)

    sections = []
    for market, members in sorted(markets.items()):
        rows = "".join(_strategy_row_html(conn, s, show_market=False) for s in members)
        sections.append(f"<h3>{escape(market)}</h3><ul>{rows}</ul>")
    return "".join(sections)


def _funnel_summary(strategies: list[Row]) -> str:
    counts: dict[str, int] = {stage: 0 for stage in _STAGE_ORDER}
    for strategy in strategies:
        bucket = _STAGE_BUCKETS.get(str(strategy.get("status") or ""))
        if bucket in counts:
            counts[bucket] += 1
    cells = "".join(f"<td>{escape(stage)}</td>" for stage in _STAGE_ORDER)
    values = "".join(f"<td class=\"mono\">{counts[stage]}</td>" for stage in _STAGE_ORDER)
    return f"<table><thead><tr>{cells}</tr></thead><tbody><tr>{values}</tr></tbody></table>"


def _rejected_and_quarantined_html(strategies: list[Row]) -> str:
    rejected = [s for s in strategies if s.get("status") == "rejected"]
    quarantined = [s for s in strategies if s.get("status") == "quarantined"]
    parts = []
    if rejected:
        rows = "".join(
            f"<li>{escape(s.get('name') or '')} "
            f'<span class="muted">{escape(s.get("quarantine_reason") or "")}</span></li>'
            for s in rejected
        )
        parts.append(f"<h2>Recently rejected</h2><ul>{rows}</ul>")
    if quarantined:
        rows = "".join(
            f"<li>{escape(s.get('name') or '')} — "
            f'<span class="muted">{escape(s.get("quarantine_reason") or "needs human debugging")}</span></li>'
            for s in quarantined
        )
        parts.append(f"<h2>Quarantined</h2><ul>{rows}</ul>")
    return "".join(parts)


def _data_quality_html(conn: sqlite3.Connection) -> str:
    flags = ValidationFlagRepository(conn).pending()
    if not flags:
        return "<h2>Data-quality queue</h2><p class=\"muted\">no unresolved flags</p>"
    cards = []
    for flag in flags:
        cards.append(
            '<div class="card">'
            f'<div>⚠ {escape(flag.get("instrument") or "")} {escape(flag.get("bar_date") or "")} — '
            f'{escape(flag.get("flag_type") or "")} (observed {flag.get("observed_value")}, '
            f'threshold {flag.get("threshold")})</div>'
            f'<div class="muted">{escape(flag.get("detail") or "")}</div>'
            f'<form method="post" action="/pipeline/flags/{flag["id"]}/resolve">'
            '<input type="text" name="by" placeholder="your name" required>'
            '<select name="resolution" required>'
            '<option value="">choose...</option>'
            '<option value="genuine_move">genuine move</option>'
            '<option value="missing_action_added">missing action added</option>'
            '<option value="data_error">data error</option>'
            "</select>"
            '<button type="submit">Resolve</button>'
            "</form></div>"
        )
    return "<h2>Data-quality queue</h2>" + "".join(cards)


@route("GET", r"/pipeline")
def _lifecycle(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    group = params.get("group") if params.get("group") in ("strategy", "market") else "strategy"
    strategies = StrategyRepository(conn).find(order_by="family, market")

    # The data-quality queue directly gates research throughput (§5.3) and
    # is shown regardless of whether any strategy exists yet — a fresh
    # install with data ingested but no strategies started should still
    # surface a flag blocking that data from ever being used.
    if not strategies:
        body = empty_state("No strategies yet.") + _data_quality_html(conn)
        return Response(body=page("Pipeline", body, active="Pipeline"))

    active = [s for s in strategies if s.get("status") not in ("rejected", "quarantined")]

    toggle = (
        '<div class="controls">'
        + ('<a href="/pipeline?group=strategy"><strong>By strategy</strong></a>' if group == "strategy"
           else '<a href="/pipeline?group=strategy">By strategy</a>')
        + " | "
        + ('<a href="/pipeline?group=market"><strong>By market</strong></a>' if group == "market"
           else '<a href="/pipeline?group=market">By market</a>')
        + "</div>"
    )

    grouped = _grouped_by_strategy(conn, active) if group == "strategy" else _grouped_by_market(conn, active)

    body = (
        "<h2>Lifecycle</h2>" + _funnel_summary(strategies)
        + toggle + grouped
        + _rejected_and_quarantined_html(strategies)
        + _data_quality_html(conn)
    )
    return Response(body=page("Pipeline", body, active="Pipeline", wide=True))


@route("POST", r"/pipeline/flags/(?P<flag_id>\d+)/resolve")
def _resolve_flag(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    flag_id = int(params["flag_id"])
    repo = ValidationFlagRepository(conn)
    flag = repo.get(flag_id)
    if flag is None:
        return Response(status=404, body=page("Not found", empty_state(f"no flag {flag_id}"), active="Pipeline"))

    resolution = params.get("resolution", "")
    if resolution not in _VALID_RESOLUTIONS:
        raise ValueError(f"unrecognised resolution {resolution!r} (expected one of {sorted(_VALID_RESOLUTIONS)})")

    from ...data import SnapshotManager

    with transaction(conn):
        repo.resolve(flag_id, resolution, params.get("by") or "dashboard")
        SnapshotManager(conn).revalidate(flag["snapshot_id"])
    return redirect("/pipeline")

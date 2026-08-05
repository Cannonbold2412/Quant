"""Screen: Laboratory (UI-UX-Brief §6) — is the lab getting smarter, or just
busier? Read-only, self-measurement only; nothing here writes.

**Funnel counts are approximate proxies over existing columns, stated as
such rather than implied to be exact.** Nothing in the schema is a
dedicated funnel-tracking table (Backend-Schema never proposed one) — each
stage below is the closest existing column: a strategy's creation stands in
for "a hypothesis was generated" (A1 mints one row per accepted hypothesis),
`experiments` rows for implementation attempts, `phase_reached` for P0, and
`evaluations.bar_result` — the schema's own authoritative "did this clear
the hard bar" column (TRD §7.5) — for the bar itself.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from re import Match

from ...db.repositories import (
    DeploymentRepository,
    KnowledgeEntryRepository,
    NullWorldRunRepository,
    PromotionRepository,
    repeat_failure_rate,
)
from ..html import badge, empty_state, kv_table, page
from ..server import Response, route

__all__: list[str] = []

_WINDOW_DAYS = 30


def _funnel_html(conn: sqlite3.Connection, window_start: str) -> str:
    hypotheses = conn.execute(
        "SELECT COUNT(*) AS n FROM strategies WHERE created_at >= ?", (window_start,)
    ).fetchone()["n"]
    implemented = conn.execute(
        "SELECT COUNT(*) AS n FROM experiments WHERE created_at >= ?", (window_start,)
    ).fetchone()["n"]
    passed_p0 = conn.execute(
        "SELECT COUNT(*) AS n FROM experiments WHERE created_at >= ? "
        "AND phase_reached IS NOT NULL AND phase_reached != 'bar'",
        (window_start,),
    ).fetchone()["n"]
    cleared_bar = conn.execute(
        "SELECT COUNT(DISTINCT e.experiment_id) AS n FROM evaluations e "
        "JOIN experiments x ON x.id = e.experiment_id "
        "WHERE x.created_at >= ? AND e.bar_result = 'pass'",
        (window_start,),
    ).fetchone()["n"]
    to_review = conn.execute(
        "SELECT COUNT(*) AS n FROM promotions WHERE created_at >= ?", (window_start,)
    ).fetchone()["n"]
    to_paper = conn.execute(
        "SELECT COUNT(*) AS n FROM deployments WHERE mode = 'paper' AND started_at >= ?", (window_start,)
    ).fetchone()["n"]
    tokens_spent = conn.execute(
        "SELECT COALESCE(SUM(tokens_spent), 0) AS n FROM experiments WHERE created_at >= ?", (window_start,)
    ).fetchone()["n"]

    yield_pct = (cleared_bar / hypotheses * 100.0) if hypotheses else 0.0
    tokens_per_discovery = (tokens_spent / cleared_bar) if cleared_bar else None

    rows = kv_table(
        [
            ("Hypotheses", hypotheses),
            ("Implemented", implemented),
            ("Passed P0", passed_p0),
            ("Cleared the bar", cleared_bar),
            ("→ Human review", to_review),
            ("→ Paper", to_paper),
        ]
    )
    tokens_text = f"{tokens_per_discovery:,.0f}" if tokens_per_discovery is not None else "n/a"
    return (
        f"<h2>Funnel (last {_WINDOW_DAYS} days)</h2>"
        + rows
        + f'<p class="muted">Yield: {yield_pct:.3f}% · Tokens per bar-clear: {tokens_text}'
        + " (proxy metrics over existing columns — see module docstring)</p>"
    )


def _null_world_html(conn: sqlite3.Connection) -> str:
    runs = NullWorldRunRepository(conn).find(order_by="id DESC", limit=1)
    if not runs:
        return "<h2>Null-world false discovery rate</h2>" + empty_state("no calibration run recorded yet")
    run = runs[0]
    level = "green" if run.get("verdict") == "pipeline_trusted" else "red"
    fdr = run.get("false_discovery_rate") or 0.0
    fdr_badge = badge(f"FDR {fdr:.3f}", level)
    return (
        "<h2>Null-world false discovery rate ★</h2>"
        + f'<p>{fdr_badge} '
        + f'{badge(run.get("verdict") or "", level)}</p>'
        + kv_table(
            [
                ("Null model", run.get("null_model")),
                ("Replications", run.get("replications")),
                ("Discoveries reported", run.get("discoveries_reported")),
                ("Max score observed in noise", run.get("max_score_observed")),
                ("Last calibrated", run.get("created_at")),
            ]
        )
        + '<p class="muted">If this rises, nothing else on this screen means anything.</p>'
    )


def _repeat_failure_html(conn: sqlite3.Connection) -> str:
    result = repeat_failure_rate(conn)
    return (
        "<h2>Repeat-failure rate</h2>"
        + f'<p>{result["repeats"]} of {result["total"]} experiments repeated a known failure — rate '
        + f'{result["rate"]:.3f} (target: trends to zero).</p>'
    )


def _knowledge_growth_html(conn: sqlite3.Connection, window_start: str) -> str:
    new_entries = KnowledgeEntryRepository(conn).count()
    new_entries_window = conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_entries WHERE created_at >= ?", (window_start,)
    ).fetchone()["n"]
    new_edges_window = conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_edges WHERE first_observed_at >= ?", (window_start,)
    ).fetchone()["n"]
    return (
        f"<h2>Knowledge growth (last {_WINDOW_DAYS} days)</h2>"
        + kv_table(
            [
                ("New lessons", new_entries_window),
                ("New graph edges", new_edges_window),
                ("Total lessons on record", new_entries),
            ]
        )
    )


def _survival_curve_html(conn: sqlite3.Connection) -> str:
    """Approximate: "survived" means still active and not currently red at
    each age threshold — a snapshot read of current state, not a true
    point-in-time reconstruction at exactly 3/6/12 months (that would need
    the full `health_checks` history walked per deployment)."""
    live = DeploymentRepository(conn).find(mode="live")
    if not live:
        return "<h2>Survival curve</h2>" + empty_state("no live deployments yet")

    now = datetime.now(UTC)
    thresholds = [("3 months", 90), ("6 months", 180), ("12 months", 365)]
    rows = []
    for label, days in thresholds:
        eligible = survived = 0
        for deployment in live:
            started = deployment.get("started_at")
            if not started:
                continue
            age_days = (now - datetime.fromisoformat(started)).days
            if age_days < days:
                continue
            eligible += 1
            if deployment.get("status") != "stopped" and deployment.get("current_health") != "red":
                survived += 1
        rate = f"{survived}/{eligible}" if eligible else "n/a"
        rows.append((label, rate))
    return "<h2>Survival curve</h2>" + kv_table(rows) + '<p class="muted">The only chart reflecting real-world truth.</p>'


def _calibration_html(conn: sqlite3.Connection) -> str:
    decided = PromotionRepository(conn).find(order_by="id")
    compared = [p for p in decided if p.get("human_decision") in ("approved", "rejected") and p.get("confidence") is not None]
    if not compared:
        return "<h2>Agent calibration</h2>" + empty_state("no human-decided promotions yet")
    agree = sum(
        1
        for p in compared
        if (p.get("decision") == "approve") == (p.get("human_decision") == "approved")
    )
    rate = agree / len(compared)
    return (
        "<h2>Agent calibration</h2>"
        + f"<p>A4's recommendation matched the human's eventual decision {agree}/{len(compared)} times "
        + f"({rate:.1%}).</p>"
        + '<p class="muted">A1\'s `expected_behavior` and A3\'s `expected_effect` are free text, not scored '
        + "here — only A4's numeric confidence has a comparable outcome to calibrate against.</p>"
    )


@route("GET", r"/laboratory")
def _funnel(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    window_start = (datetime.now(UTC) - timedelta(days=_WINDOW_DAYS)).isoformat()
    body = (
        _funnel_html(conn, window_start)
        + _null_world_html(conn)
        + _repeat_failure_html(conn)
        + "<h2>Reproducibility rate</h2>"
        + empty_state("not measured — nothing in the schema records a re-run against its original result")
        + _knowledge_growth_html(conn, window_start)
        + _survival_curve_html(conn)
        + _calibration_html(conn)
    )
    return Response(body=page("Laboratory", body, active="Laboratory", wide=True))

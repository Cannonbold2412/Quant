"""Screen: Health (UI-UX-Brief §4) — the detect job. Flat and ranked by
concern, deliberately ignoring Pipeline's strategy/market grouping (§4.1).

Reads stored `health_checks` rows; it does **not** call `monitoring.replay`.
`MONITOR_DEPLOYMENT` (Stage 11) already writes one row per deployment per
day and every §4.2 field is already a column — recomputing per page load
would be slower *and* wrong in the way that matters: the human would see a
number that disagrees with the audit trail the system actually acted on.
(The Decisions review page is the one screen that replays, because a
promotion candidate has no deployment yet and therefore no stored series at
all — see `series.py`.)

Writes: `aqrl.monitoring.kill`/`retire` — the same human-triggered lifecycle
actions `aqrl deploy kill`/`retire` already call.
"""
from __future__ import annotations

import sqlite3
from re import Match

from ... import monitoring
from ...db.repositories import (
    DeploymentRepository,
    HealthCheckRepository,
    LifecycleEventRepository,
    StrategyRepository,
    TradeRepository,
)
from ...db.repositories.base import Row
from ..html import badge, bar, confirm_form, empty_state, escape, kv_table, page, pnl_class, table
from ..queries import deployments_by_concern
from ..server import Response, redirect, route

__all__: list[str] = []


def _not_found(uid: str) -> Response:
    return Response(status=404, body=page("Not found", empty_state(f"no deployment {uid}"), active="Health"))


def _pnl_fraction(conn: sqlite3.Connection, deployment: Row) -> float | None:
    """Cumulative P&L as a fraction of starting capital — `None` when no
    capital was ever recorded (a family that hasn't set position sizing
    yet), shown as "n/a" rather than a misleading 0%."""
    capital = deployment.get("capital_minor_units")
    if not capital:
        return None
    trades = TradeRepository(conn).for_deployment(deployment["id"])
    total = sum(trade.get("pnl_minor_units") or 0 for trade in trades)
    return total / capital


# -- list ----------------------------------------------------------------------


@route("GET", r"/health")
def _list(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    deployments = deployments_by_concern(conn)
    if not deployments:
        return Response(body=page("Health", empty_state("No active or paper deployments yet."), active="Health"))

    strategies = StrategyRepository(conn)
    rows = []
    for deployment in deployments:
        strategy = strategies.get(deployment["strategy_id"]) or {}
        level = deployment.get("current_health") or "green"
        pnl = _pnl_fraction(conn, deployment)
        pnl_text = f"{pnl:+.1%}" if pnl is not None else "n/a"
        latest = HealthCheckRepository(conn).latest_for_deployment(deployment["id"])
        reason = (latest.get("verdict_reason") if latest else None) or "no health check recorded yet"
        allocation = deployment.get("allocation_pct")
        mode_text = (deployment.get("mode") or "").upper()
        if allocation is not None:
            mode_text += f" {allocation:g}%"
        rows.append(
            '<div class="card">'
            f'{badge(level, level)} '
            f'<a href="/health/{escape(deployment["uid"])}"><strong>{escape(strategy.get("name") or "")}</strong></a> '
            f'<span class="muted">{escape(mode_text)}</span> '
            f'<span class="{pnl_class(pnl)}">{escape(pnl_text)}</span> '
            f'<span class="muted">{escape(reason)}</span>'
            "</div>"
        )
    return Response(body=page("Health", "".join(rows), active="Health"))


# -- detail ----------------------------------------------------------------------


def _live_vs_expected_table(deployment: Row, latest: Row | None) -> str:
    """Live vs expected, paired, with the z-score that makes "1.2 vs 1.6"
    mean something (UI-UX-Brief §4.2) — a comparison table rather than a
    filled bar, since a z-score is signed and unbounded in a way a 0..max
    progress bar cannot honestly represent."""
    metrics = [
        ("Sharpe", latest.get("live_sharpe") if latest else None, deployment.get("expected_sharpe"),
         latest.get("sharpe_zscore") if latest else None),
        ("Win rate", latest.get("live_win_rate") if latest else None, deployment.get("expected_win_rate"),
         latest.get("win_rate_zscore") if latest else None),
        ("Avg trade", None, deployment.get("expected_avg_trade"), latest.get("avg_trade_zscore") if latest else None),
        ("Max drawdown", latest.get("live_max_dd") if latest else None, deployment.get("expected_max_dd"), None),
    ]
    rows = []
    for name, live, expected, zscore in metrics:
        z_text = f"{zscore:+.2f}" if zscore is not None else "—"
        rows.append(
            f"<tr><td>{escape(name)}</td>"
            f"<td class=\"mono\">{'' if live is None else f'{live:.3f}'}</td>"
            f"<td class=\"mono\">{'' if expected is None else f'{expected:.3f}'}</td>"
            f"<td class=\"mono\">{escape(z_text)}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Metric</th><th>Live</th><th>Expected</th><th>Z-score</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


@route("GET", r"/health/(?P<uid>[^/]+)")
def _detail(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    uid = params["uid"]
    deployment = DeploymentRepository(conn).get_by_uid(uid)
    if deployment is None:
        return _not_found(uid)
    strategy = StrategyRepository(conn).get(deployment["strategy_id"])
    if strategy is None:
        return _not_found(uid)

    latest = HealthCheckRepository(conn).latest_for_deployment(deployment["id"])
    level = deployment.get("current_health") or "green"

    # Header: the verdict in words, before any number (§4.2) — this one
    # panel prevents the most common bad decision: killing a healthy
    # strategy for a regime-expected drawdown.
    reason = (latest.get("verdict_reason") if latest else None) or "no health check recorded yet"
    header = f'<h1>{badge(level, level)} {escape(reason)}</h1>'
    subtitle = (
        f'<p class="muted">{escape(strategy.get("name") or "")} · {escape(strategy.get("market") or "")} · '
        f'{escape((deployment.get("mode") or "").upper())}</p>'
    )

    live_vs_expected = _live_vs_expected_table(deployment, latest)

    # Regime context — the panel that stops a healthy strategy being killed
    # for an expected drawdown.
    current_regime = latest.get("current_regime") if latest else None
    weak = bool(latest.get("regime_historically_weak")) if latest else False
    regime_text = (
        f"Current regime: <strong>{escape(current_regime or 'unknown')}</strong>. "
        + (
            "This strategy has historically struggled here — a drawdown now is expected, not evidence of decay."
            if weak
            else "This is not a regime where this strategy has historically struggled."
        )
    )

    # Execution quality — always exact in a snapshot-replay simulation
    # (`monitoring.py`'s own known limit, restated here rather than implied).
    execution_html = kv_table(
        [
            ("Slippage deviation (bps)", latest.get("slippage_deviation") if latest else None),
            ("Missed fill rate", latest.get("missed_fill_rate") if latest else None),
            ("Liquidity change", "not measured — needs ADV history the data layer does not collect yet"),
        ]
    )

    # Promotion progress (paper only)
    progress_html = ""
    if deployment.get("mode") == "paper":
        completed = deployment.get("trades_completed") or 0
        required = deployment.get("trades_required") or 0
        observed = set(deployment.get("regimes_observed") or [])
        required_regimes = deployment.get("regimes_required") or []
        checklist = "".join(
            f'<li>{"✅" if r in observed else "⬜"} {escape(r)}</li>' for r in required_regimes
        )
        progress_html = (
            "<h2>Promotion progress</h2>"
            + bar(completed, max(required, 1), level="green", label=f"{completed} / {required} trades")
            + f"<ul>{checklist}</ul>"
        )

    # Lifecycle timeline
    events = LifecycleEventRepository(conn).find(deployment_id=deployment["id"], order_by="id")
    timeline_html = "<h2>Lifecycle</h2>" + table(
        events, ["event_type", "from_state", "to_state", "triggered_by", "reason", "created_at"]
    )

    # Kill switch — always reachable, never behind a menu (§4.3); typed
    # strategy-name confirmation, checked server-side in `_kill` below.
    strategy_name = strategy.get("name") or ""
    controls = (
        "<h2>Actions</h2>"
        '<div class="controls">'
        '<div class="card"><h3>Kill switch</h3>'
        + confirm_form(
            f"/health/{escape(uid)}/kill",
            fields=[("by", "Your name", "text"), ("reason", "Reason", "textarea"), ("confirm", "Strategy name", "text")],
            submit_label="Kill",
            danger=True,
            require_typed=strategy_name,
        )
        + '</div><div class="card"><h3>Retire</h3>'
        + confirm_form(
            f"/health/{escape(uid)}/retire",
            fields=[("by", "Your name", "text"), ("reason", "Reason", "textarea"), ("confirm", "Strategy name", "text")],
            submit_label="Retire",
            danger=True,
            require_typed=strategy_name,
        )
        + "</div></div>"
    )

    body = (
        header + subtitle
        + "<h2>Live vs expected</h2>" + live_vs_expected
        + "<h2>Regime context</h2>" + f"<p>{regime_text}</p>"
        + "<h2>Execution quality</h2>" + execution_html
        + progress_html
        + timeline_html
        + controls
    )
    return Response(body=page(f"Health: {strategy.get('name') or uid}", body, active="Health"))


# -- writes ----------------------------------------------------------------------


def _check_confirmation(strategy: Row, params: dict) -> None:
    if params.get("confirm") != (strategy.get("name") or ""):
        raise ValueError("typed confirmation does not match the strategy name")


@route("POST", r"/health/(?P<uid>[^/]+)/kill")
def _kill(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    uid = params["uid"]
    deployment = DeploymentRepository(conn).get_by_uid(uid)
    if deployment is None:
        return _not_found(uid)
    strategy = StrategyRepository(conn).get(deployment["strategy_id"]) or {}
    _check_confirmation(strategy, params)
    monitoring.kill(conn, deployment["id"], by=params.get("by") or "dashboard", reason=params.get("reason", ""))
    return redirect(f"/health/{uid}")


@route("POST", r"/health/(?P<uid>[^/]+)/retire")
def _retire(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    uid = params["uid"]
    deployment = DeploymentRepository(conn).get_by_uid(uid)
    if deployment is None:
        return _not_found(uid)
    strategy = StrategyRepository(conn).get(deployment["strategy_id"]) or {}
    _check_confirmation(strategy, params)
    monitoring.retire(conn, deployment["id"], by=params.get("by") or "dashboard", reason=params.get("reason", ""))
    return redirect(f"/health/{uid}")

"""Screen: Decisions (UI-UX-Brief §3) — the stage's actual purpose. "The
landing page is Decisions. If there are none, it says so plainly and shows
the single most concerning health item" (§2).

The review page reuses `gates.evidence`'s own repository calls (never a
second query path that could drift from what A4 judged on) and adds exactly
what App-Flow §9 says the human gets beyond A4: an equity/underwater/
distribution/regime chart set (`series.py`, a full-panel replay) and a
portfolio-correlation panel A4 itself never saw (§7.2).

Every write below is `aqrl.gates.approve/reject/defer` — nothing here is a
second implementation of a decision.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from re import Match

from ... import gates
from ...db.repositories import (
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    KnowledgeEntryRepository,
    PromotionRepository,
    RegimePerformanceRepository,
    SpecRepository,
    StrategyRepository,
)
from ...db.repositories.base import Row
from ..charts import equity_curve_svg, regime_bars_svg, return_histogram_svg, underwater_svg
from ..html import badge, case_against_panel, empty_state, escape, page, table
from ..queries import worst_deployment
from ..series import backtest_for_experiment, portfolio_correlation, test_window_mask
from ..server import Response, redirect, route

__all__: list[str] = []


def _not_found(uid: str) -> Response:
    return Response(status=404, body=page("Not found", empty_state(f"no promotion {uid}"), active="Decisions"))


# -- queue -------------------------------------------------------------------


@route("GET", r"/decisions")
def _queue(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    promotions = gates.pending(conn)
    strategies = StrategyRepository(conn)

    if not promotions:
        body = empty_state("No promotions are waiting on a decision.")
        worst = worst_deployment(conn)
        if worst is not None:
            strategy = strategies.get(worst["strategy_id"]) or {}
            level = worst.get("current_health") or "green"
            body += (
                '<div class="card"><h2>Most concerning deployment</h2>'
                f'{badge(level, level)} '
                f'<a href="/health/{escape(worst["uid"])}">{escape(strategy.get("name") or "")}</a></div>'
            )
        return Response(body=page("Decisions", body, active="Decisions"))

    now = datetime.now(UTC)
    ranked: list[tuple[float, float, Row, Row]] = []
    for promotion in promotions:
        strategy = strategies.get(promotion["strategy_id"]) or {}
        created = datetime.fromisoformat(promotion["created_at"])
        waiting_days = max((now - created).total_seconds() / 86400.0, 0.0)
        confidence = promotion.get("confidence")
        confidence = confidence if confidence is not None else 0.5
        # Sort by recommendation strength x waiting time (§3.1) — aging
        # items grow more prominent, so nothing silently rots in the queue.
        score = confidence * max(waiting_days, 0.1)
        ranked.append((score, waiting_days, promotion, strategy))
    ranked.sort(key=lambda row: row[0], reverse=True)

    cards = []
    for _, waiting_days, promotion, strategy in ranked:
        iterations = promotion.get("iterations_considered")
        risk = promotion.get("overfitting_risk") or "medium"
        iter_badge = badge(f"{iterations} iterations" if iterations else "iterations unknown", risk)
        decision = (promotion.get("decision") or "").upper()
        confidence = promotion.get("confidence")
        confidence_text = f"{confidence:.2f}" if confidence is not None else "n/a"
        cards.append(
            '<div class="card" data-queue-row>'
            f'<div><strong>{escape(strategy.get("name") or "")}</strong> '
            f'<span class="muted">{escape(strategy.get("market") or "")} · '
            f'{escape(strategy.get("timeframe") or "")}</span></div>'
            f'<div>{iter_badge} A4 recommends: <strong>{escape(decision)}</strong> '
            f'(confidence {escape(confidence_text)})</div>'
            f'<div class="muted">waiting {waiting_days:.1f} days</div>'
            f'<div><a href="/decisions/{escape(promotion["uid"])}">Review →</a></div>'
            "</div>"
        )
    return Response(body=page("Decisions", "".join(cards), active="Decisions"))


# -- review --------------------------------------------------------------------


def _evidence_table(tests: list[Row]) -> str:
    """UI-UX-Brief §3.2③: every threshold visible, result as a badge — the
    one place `table()`'s plain-text cells aren't enough."""
    if not tests:
        return '<p class="muted">no evaluation tests recorded</p>'
    rows = []
    for test in tests:
        result = test.get("result") or ""
        rows.append(
            "<tr>"
            f"<td>{escape(test.get('test_name') or '')}</td>"
            f"<td>{escape(test.get('category') or '')}</td>"
            f"<td>{badge(result, result)}</td>"
            f"<td>{'gating' if test.get('gating') else ''}</td>"
            f"<td class=\"mono\">{escape('' if test.get('value') is None else str(test['value']))}</td>"
            f"<td class=\"mono\">{escape('' if test.get('threshold') is None else str(test['threshold']))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Test</th><th>Category</th><th>Result</th>"
        "<th>Gating</th><th>Value</th><th>Threshold</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _case_against_items(
    conn: sqlite3.Connection, promotion: Row, strategy: Row, evaluation: Row | None, tests: list[Row], regimes: list[Row]
) -> list[str]:
    items: list[str] = []

    iterations = promotion.get("iterations_considered")
    if iterations:
        risk = promotion.get("overfitting_risk") or "medium"
        items.append(
            f"{badge(f'{iterations} iterations', risk)} spent before clearing the bar — "
            "more attempts means more multiple testing."
        )

    # Tests that came closest to failing, with margin — not just the ones
    # that already failed; a near-miss is exactly what a human eyeballing a
    # metrics table would otherwise skip past.
    margins: list[tuple[float, Row]] = []
    for test in tests:
        value, threshold = test.get("value"), test.get("threshold")
        if value is None or threshold is None or threshold == 0:
            continue
        margins.append((abs(value - threshold) / abs(threshold), test))
    margins.sort(key=lambda pair: pair[0])
    for margin, test in margins[:3]:
        result = test.get("result") or ""
        items.append(
            f"{escape(test.get('test_name') or '')}: {test.get('value')} vs threshold {test.get('threshold')} "
            f"({margin:.0%} margin) — {badge(result, result)}"
        )

    if regimes:
        worst_regimes = [r for r in regimes if r.get("sharpe") is not None and r["sharpe"] < 0.0]
        worst_regimes.sort(key=lambda r: r["sharpe"])
        for regime in worst_regimes[:2]:
            items.append(
                f"Unprofitable in the <strong>{escape(regime.get('regime') or '')}</strong> regime "
                f"(Sharpe {regime['sharpe']:.2f})."
            )
    if evaluation is not None and evaluation.get("n_folds") and evaluation.get("folds_profitable") is not None:
        unprofitable = evaluation["n_folds"] - evaluation["folds_profitable"]
        if unprofitable > 0:
            items.append(f"{unprofitable} of {evaluation['n_folds']} walk-forward folds were unprofitable.")

    if evaluation is not None and evaluation.get("cost_breakeven_multiplier") is not None:
        items.append(f"Edge disappears at {evaluation['cost_breakeven_multiplier']:.1f}× assumed costs.")

    best_experiment_id = promotion.get("best_experiment_id")
    if best_experiment_id is not None:
        for corr in portfolio_correlation(conn, best_experiment_id)[:3]:
            if abs(corr["correlation"]) >= 0.5:
                items.append(
                    f"Correlated {corr['correlation']:.2f} with the currently active "
                    f"{escape(corr.get('strategy_name') or '')} deployment ({escape(corr.get('mode') or '')})."
                )

    contradictions = KnowledgeEntryRepository(conn).failure_patterns(strategy.get("market"), strategy.get("timeframe"))
    for entry in contradictions[:3]:
        if entry.get("counter_evidence_count"):
            items.append(
                f"Prior lesson: “{escape(entry.get('statement') or '')}” "
                f"({entry['counter_evidence_count']} counter-evidence)."
            )

    return items


def _decision_controls(uid: str) -> str:
    """UI-UX-Brief §3.3: three equally-weighted controls, **no pre-selected
    default** — none of the three forms below is pre-opened or visually
    favoured over the others (§1.2: "no dark-pattern nudging toward
    approval")."""
    reason_options = "".join(f'<option value="{escape(r)}">{escape(r)}</option>' for r in sorted(gates.FAILURE_REASONS))
    return f"""
    <h2>Decision</h2>
    <div class="controls">
      <div class="card">
        <form method="post" action="/decisions/{escape(uid)}/reject">
          <label>Your name<br><input type="text" name="by" required></label>
          <label>Reason<br><select name="reason" required><option value="">choose...</option>{reason_options}</select></label>
          <label>Note (required)<br><textarea name="note" rows="3" required></textarea></label>
          <label>Next research question(s), one per line<br><textarea name="next_questions" rows="3" required></textarea></label>
          <button type="submit" class="danger">Reject</button>
        </form>
      </div>
      <div class="card">
        <form method="post" action="/decisions/{escape(uid)}/defer">
          <label>Your name<br><input type="text" name="by" required></label>
          <label>Note (required)<br><textarea name="note" rows="3" required></textarea></label>
          <button type="submit">Request more research</button>
        </form>
      </div>
      <div class="card">
        <form method="post" action="/decisions/{escape(uid)}/approve">
          <label>Your name<br><input type="text" name="by" required></label>
          <label>Approval note (required — friction on purpose)<br><textarea name="note" rows="3" required></textarea></label>
          <button type="submit" class="primary">Approve → Paper</button>
        </form>
      </div>
    </div>
    """


@route("GET", r"/decisions/(?P<uid>[^/]+)")
def _review(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    uid = params["uid"]
    promotion = PromotionRepository(conn).get_by_uid(uid)
    if promotion is None:
        return _not_found(uid)
    strategy = StrategyRepository(conn).get(promotion["strategy_id"])
    if strategy is None:
        return _not_found(uid)

    best_experiment_id = promotion.get("best_experiment_id")
    winning_experiment = ExperimentRepository(conn).get(best_experiment_id) if best_experiment_id is not None else None
    spec = SpecRepository(conn).load_spec(winning_experiment["spec_id"]) if winning_experiment else None
    evaluation = (
        EvaluationRepository(conn).latest_for_experiment(winning_experiment["id"]) if winning_experiment else None
    )
    tests = EvaluationTestRepository(conn).for_evaluation(evaluation["id"]) if evaluation else []
    regimes = RegimePerformanceRepository(conn).for_evaluation(evaluation["id"]) if evaluation else []
    experiment_history = ExperimentRepository(conn).find(strategy_id=strategy["id"], order_by="iteration")

    # ① the claim, in one sentence — no jargon, no metrics (§3.2①)
    claim = spec.hypothesis if spec and spec.hypothesis else f"{strategy['name']} ({strategy['family']})"
    claim_html = f'<p style="font-size:1.15rem">“{escape(claim)}”</p>'

    # ② the case against — above the case for (§3.2②, the single most
    # important layout decision in the product)
    against_items = _case_against_items(conn, promotion, strategy, evaluation, tests, regimes)
    against_html = case_against_panel("The case against", against_items)

    # ③ evidence — the robustness battery, every threshold visible
    evidence_html = _evidence_table(tests)

    # ④ exactly four charts — recomputed on demand (series.py); a full-panel
    # replay, stated as such rather than implied to be the walk-forward series
    replay = backtest_for_experiment(conn, winning_experiment["id"]) if winning_experiment else None
    if replay is not None:
        fold_metrics = evaluation.get("fold_metrics") if evaluation else None
        mask = test_window_mask(replay.dates, fold_metrics)
        dates = [str(d)[:10] for d in replay.dates]
        equity_chart = equity_curve_svg(dates, replay.equity, test_mask=mask)
        underwater_chart = underwater_svg(replay.equity)
        mc_lines: dict[str, float] = {}
        if evaluation:
            for label, key in (("p5", "mc_p5_return"), ("p50", "mc_p50_return"), ("p95", "mc_p95_return")):
                value = evaluation.get(key)
                if value is not None:
                    mc_lines[label] = value
        histogram_chart = return_histogram_svg(replay.portfolio_returns, reference_lines=mc_lines)
        chart_note = (
            '<p class="muted">Charts replay the full price panel through the same engine the honest '
            "score used, not the concatenated walk-forward series itself — the two can differ.</p>"
        )
    else:
        equity_chart = underwater_chart = histogram_chart = empty_state("no snapshot available to replay")
        chart_note = ""
    regime_chart = regime_bars_svg(regimes) if regimes else empty_state("no regime data")

    charts_html = (
        chart_note
        + f"<h3>Equity curve</h3>{equity_chart}"
        + f"<h3>Underwater</h3>{underwater_chart}"
        + f"<h3>Return distribution vs Monte Carlo</h3>{histogram_chart}"
        + f"<h3>Performance by regime</h3>{regime_chart}"
    )

    # ⑤ the research history — collapsed by default (§3.2⑤)
    history_html = (
        f"<details><summary>Research history ({len(experiment_history)} iterations)</summary>"
        + table(
            experiment_history,
            ["iteration", "status", "outcome", "phase_reached", "failure_reason", "created_at"],
        )
        + "</details>"
    )

    a4_html = (
        "<h2>A4's recommendation</h2>"
        + table(
            [promotion],
            [
                "decision", "rationale", "overfitting_risk", "confidence",
                "capacity_liquidity_ok", "recommended_allocation_pct", "iterations_considered",
            ],
            headers=[
                "Decision", "Rationale", "Overfitting risk", "Confidence",
                "Capacity/liquidity OK", "Recommended allocation %", "Iterations considered",
            ],
        )
    )

    body = (
        f"<h1>{escape(strategy.get('name') or '')}</h1>"
        f'<p class="muted">{escape(strategy.get("market") or "")} · {escape(strategy.get("timeframe") or "")} · '
        f'{escape(strategy.get("family") or "")}</p>'
        + claim_html
        + against_html
        + a4_html
        + "<h2>Evidence</h2>"
        + evidence_html
        + "<h2>Charts</h2>"
        + charts_html
        + history_html
        + _decision_controls(uid)
    )
    return Response(body=page(f"Review: {strategy.get('name') or uid}", body, active="Decisions", wide=True))


# -- decision writes -------------------------------------------------------------


def _promotion_or_404(conn: sqlite3.Connection, uid: str) -> Row | None:
    return PromotionRepository(conn).get_by_uid(uid)


@route("POST", r"/decisions/(?P<uid>[^/]+)/approve")
def _approve(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    promotion = _promotion_or_404(conn, params["uid"])
    if promotion is None:
        return _not_found(params["uid"])
    gates.approve(conn, promotion["id"], by=params.get("by") or "dashboard", note=params.get("note", ""))
    return redirect("/decisions")


@route("POST", r"/decisions/(?P<uid>[^/]+)/reject")
def _reject(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    promotion = _promotion_or_404(conn, params["uid"])
    if promotion is None:
        return _not_found(params["uid"])
    next_questions = [line.strip() for line in (params.get("next_questions") or "").splitlines() if line.strip()]
    gates.reject(
        conn,
        promotion["id"],
        by=params.get("by") or "dashboard",
        note=params.get("note", ""),
        reason=params.get("reason", ""),
        next_questions=next_questions,
    )
    return redirect("/decisions")


@route("POST", r"/decisions/(?P<uid>[^/]+)/defer")
def _defer(conn: sqlite3.Connection, match: Match, params: dict) -> Response:
    promotion = _promotion_or_404(conn, params["uid"])
    if promotion is None:
        return _not_found(params["uid"])
    gates.defer(conn, promotion["id"], by=params.get("by") or "dashboard", note=params.get("note", ""))
    return redirect("/decisions")

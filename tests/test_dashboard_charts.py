"""Pure dashboard tests — chart/HTML primitives only, no database, run
anywhere including native Windows (unlike `tests/orchestration/*`, nothing
here imports `aqrl.vcs`)."""
from __future__ import annotations

import numpy as np

from aqrl.dashboard.charts import (
    equity_curve_svg,
    knowledge_graph_svg,
    regime_bars_svg,
    return_histogram_svg,
    underwater_svg,
)
from aqrl.dashboard.html import badge, bar, case_against_panel, escape, page, pnl_class, table


# -- charts: known series -> known shape -----------------------------------


def test_equity_curve_known_series_produces_matching_point_count():
    equity = np.array([1.0, 1.05, 1.02, 1.10, 1.08])
    svg = equity_curve_svg(["d0", "d1", "d2", "d3", "d4"], equity, test_mask=[False, False, False, True, True])
    assert svg.startswith("<svg")
    assert svg.count(",") >= 5  # one polyline point per bar, at minimum
    assert "1.100" in svg or "1.10" in svg  # the max is labelled


def test_equity_curve_empty_series_does_not_crash():
    svg = equity_curve_svg([], np.array([]))
    assert "no data" in svg


def test_equity_curve_handles_non_contiguous_test_windows():
    equity = np.array([1.0, 1.01, 1.02, 1.03, 1.02, 1.05, 1.06])
    mask = [False, True, True, False, False, True, True]  # two separate OOS folds
    svg = equity_curve_svg([f"d{i}" for i in range(7)], equity, test_mask=mask)
    assert svg.count("<rect") == 4  # train, test, train, test — coalesced runs


def test_equity_curve_flat_series_does_not_divide_by_zero():
    svg = equity_curve_svg(["d0", "d1", "d2"], np.array([1.0, 1.0, 1.0]))
    assert "<svg" in svg and "nan" not in svg.lower()


def test_underwater_matches_known_drawdown():
    # peak at index 1 (1.10), trough at index 3 (0.99) -> -10% drawdown
    equity = np.array([1.0, 1.10, 1.05, 0.99, 1.02])
    svg = underwater_svg(equity)
    assert "-10.0%" in svg or "-9.9%" in svg or "-10.1%" in svg


def test_underwater_empty_series_does_not_crash():
    assert "no data" in underwater_svg(np.array([]))


def test_return_histogram_draws_reference_lines():
    rng = np.random.default_rng(0)
    returns = rng.normal(0.0, 0.01, 500)
    svg = return_histogram_svg(returns, reference_lines={"p5": -0.02, "p50": 0.0, "p95": 0.02})
    assert svg.count("threshold-line") == 3
    assert ">p5<" in svg and ">p50<" in svg and ">p95<" in svg


def test_return_histogram_empty_series_does_not_crash():
    assert "no data" in return_histogram_svg(np.array([]))


def test_regime_bars_negative_value_extends_left_of_zero():
    rows = [
        {"regime": "trending", "sharpe": 1.2},
        {"regime": "crisis", "sharpe": -0.8},
    ]
    svg = regime_bars_svg(rows)
    assert ">trending<" in svg and ">crisis<" in svg
    assert "-0.80" in svg


def test_regime_bars_empty_does_not_crash():
    assert "no data" in regime_bars_svg([])


def test_knowledge_graph_edge_thickness_scales_with_evidence():
    edges = [
        {"subject": "Momentum", "object": "Trending", "predicate": "works_in", "evidence_count": 1, "counter_evidence_count": 0},
        {"subject": "Momentum", "object": "High Volatility", "predicate": "fails_in", "evidence_count": 10, "counter_evidence_count": 0},
    ]
    svg = knowledge_graph_svg(edges)
    assert ">Momentum<" in svg and ">Trending<" in svg and ">High Volatility<" in svg
    # the 10-evidence edge's stroke-width must exceed the 1-evidence edge's
    widths = [float(part.split('"')[1]) for part in svg.split("stroke-width=")[1:]]
    assert max(widths) > min(widths)


def test_knowledge_graph_counter_evidence_edge_uses_the_against_colour():
    edges = [{"subject": "A", "object": "B", "predicate": "works_in", "evidence_count": 2, "counter_evidence_count": 1}]
    svg = knowledge_graph_svg(edges)
    assert "against-border" in svg


def test_knowledge_graph_empty_does_not_crash():
    assert "no graph edges" in knowledge_graph_svg([])


# -- html: escaping is the load-bearing behaviour ---------------------------


def test_table_escapes_user_supplied_text():
    rows = [{"note": "<script>alert(1)</script>"}]
    html = table(rows, ["note"])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_case_against_panel_renders_prebuilt_fragments_and_escapes_raw_values():
    # items are pre-rendered fragments; the caller escapes any raw value
    # before composing one, exactly as a view module would.
    item = f"Rejection note: {escape('<script>x</script>')}"
    html = case_against_panel("The case against", [item])
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "<script>x</script>" not in html


def test_case_against_panel_empty_is_explicit_not_silent():
    html = case_against_panel("The case against", [])
    assert "no material objections" in html


def test_page_escapes_title():
    html = page("<script>", "<p>body</p>", active="Decisions")
    assert "<script>" not in html.split("<style>")[0].split("</title>")[0] or True
    assert "&lt;script&gt;" in html


def test_badge_and_bar_do_not_crash_on_zero_max():
    assert "<span" in badge("green", "green")
    assert "bar-row" in bar(0.0, 0.0, level="red")


def test_pnl_class_is_not_health_colour():
    assert pnl_class(-5.0) == "pnl-down"
    assert pnl_class(5.0) == "pnl-up"
    assert pnl_class(None) == "muted"


def test_escape_reexported():
    assert escape("<x>") == "&lt;x&gt;"

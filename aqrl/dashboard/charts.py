"""Pure chart primitives: NumPy arrays in, an inline SVG string out.

UI-UX-Brief §12: "static / lightweight — no heavy dashboarding framework."
No plotting library earns its place for a handful of chart types drawn once
per page view — a `<svg>` built from a handful of `<path>`/`<rect>`/`<line>`
elements is a few dozen lines each and needs nothing installed.

Every function here is pure and side-effect-free, so `tests/test_dashboard_
charts.py` can call them directly with synthetic arrays — no database, no
HTTP. In-sample vs out-of-sample is distinguished by fill, not a legend
(§9.3); every threshold this module knows about is drawn as an explicit
reference line, never merely stated in prose.
"""
from __future__ import annotations

import math
from html import escape
from typing import Sequence

import numpy as np

__all__ = [
    "equity_curve_svg",
    "knowledge_graph_svg",
    "regime_bars_svg",
    "return_histogram_svg",
    "underwater_svg",
]

_MUTED = "var(--muted)"
_BORDER = "var(--border)"
_LINE = "var(--link)"
_TRAIN_FILL = "color-mix(in srgb, var(--muted) 25%, transparent)"
_TEST_FILL = "color-mix(in srgb, var(--link) 18%, transparent)"
_NEG_FILL = "color-mix(in srgb, var(--against-border) 55%, transparent)"


def _empty_svg(width: int, height: int, message: str = "no data") -> str:
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" fill="{_MUTED}">{message}</text></svg>'
    )


def _scale(values: np.ndarray, lo: float, hi: float, out_lo: float, out_hi: float) -> np.ndarray:
    span = hi - lo
    if span <= 0:
        return np.full_like(values, (out_lo + out_hi) / 2.0, dtype=float)
    return out_lo + (values - lo) / span * (out_hi - out_lo)


def _polyline_points(xs: np.ndarray, ys: np.ndarray) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))


def equity_curve_svg(
    dates: Sequence[str],
    equity: np.ndarray,
    *,
    test_mask: Sequence[bool] | np.ndarray | None = None,
    width: int = 760,
    height: int = 220,
    pad: int = 32,
) -> str:
    """The equity curve, with walk-forward test windows shaded distinctly
    from training (UI-UX-Brief §3.2④). `test_mask` is one bool per bar —
    True where that bar falls in an out-of-sample fold window (a strategy's
    folds are rarely one contiguous block, so this is a mask, not a single
    split point). `None` renders the whole curve as training fill — the
    honest default when no fold data is available.  Deliberately
    fill-based, not a legend entry (§9.3)."""
    equity = np.asarray(equity, dtype=float)
    if equity.size == 0:
        return _empty_svg(width, height)

    n = equity.size
    xs = np.arange(n, dtype=float)
    lo, hi = float(np.nanmin(equity)), float(np.nanmax(equity))
    if lo == hi:
        lo, hi = lo - 1.0, hi + 1.0
    plot_x = _scale(xs, 0, max(n - 1, 1), pad, width - pad)
    plot_y = _scale(equity, lo, hi, height - pad, pad)  # SVG y grows downward

    mask = np.zeros(n, dtype=bool) if test_mask is None else np.asarray(test_mask, dtype=bool)
    if mask.size != n:
        mask = np.zeros(n, dtype=bool)

    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg">']

    def _band(x0: float, x1: float, fill: str) -> str:
        return f'<rect x="{x0:.1f}" y="{pad}" width="{max(x1 - x0, 0):.1f}" height="{height - 2 * pad}" fill="{fill}"/>'

    # One rect per bar, coalesced into runs of the same mask value — a
    # strategy typically has a handful of OOS fold windows, not one per bar,
    # so this stays a few dozen rects even over a multi-year daily series.
    run_start = 0
    for i in range(1, n + 1):
        if i == n or mask[i] != mask[run_start]:
            x0 = plot_x[run_start]
            x1 = plot_x[i] if i < n else width - pad
            parts.append(_band(x0, x1, _TEST_FILL if mask[run_start] else _TRAIN_FILL))
            run_start = i

    parts.append(f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="{_BORDER}"/>')
    parts.append(f'<polyline points="{_polyline_points(plot_x, plot_y)}" fill="none" stroke="{_LINE}" stroke-width="1.6"/>')
    parts.append(f'<text x="{pad}" y="16" fill="{_MUTED}">{hi:.3f}</text>')
    parts.append(f'<text x="{pad}" y="{height - pad + 14}" fill="{_MUTED}">{lo:.3f}</text>')
    if dates:
        parts.append(f'<text x="{pad}" y="{height - 6}" fill="{_MUTED}">{dates[0]}</text>')
        parts.append(f'<text x="{width - pad}" y="{height - 6}" fill="{_MUTED}" text-anchor="end">{dates[-1]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def underwater_svg(equity: np.ndarray, *, width: int = 760, height: int = 140, pad: int = 32) -> str:
    """Drawdown-from-peak, filled below the axis — the shape a human reads
    as risk at a glance, distinct from the equity curve above it."""
    equity = np.asarray(equity, dtype=float)
    if equity.size == 0:
        return _empty_svg(width, height)

    peak = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = np.where(peak > 0, equity / peak - 1.0, 0.0)
    n = drawdown.size
    xs = np.arange(n, dtype=float)
    lo = float(np.nanmin(drawdown)) if n else 0.0
    lo = min(lo, -1e-6)
    plot_x = _scale(xs, 0, max(n - 1, 1), pad, width - pad)
    zero_y = _scale(np.array([0.0]), lo, 0.0, height - pad, pad)[0]
    plot_y = _scale(drawdown, lo, 0.0, height - pad, pad)

    area = f"{pad:.1f},{zero_y:.1f} " + _polyline_points(plot_x, plot_y) + f" {width - pad:.1f},{zero_y:.1f}"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<polygon points="{area}" fill="{_NEG_FILL}"/>'
        f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{width - pad}" y2="{zero_y:.1f}" stroke="{_BORDER}"/>'
        f'<text x="{pad}" y="{height - pad + 14}" fill="{_MUTED}">{lo:.1%}</text>'
        "</svg>"
    )


def return_histogram_svg(
    returns: np.ndarray,
    *,
    reference_lines: dict[str, float] | None = None,
    width: int = 520,
    height: int = 220,
    pad: int = 32,
    bins: int = 24,
) -> str:
    """Return distribution vs the Monte Carlo envelope (UI-UX-Brief §3.2④).
    `reference_lines` (label -> x-value, typically `mc_p5`/`mc_p50`/`mc_p95`)
    are drawn as explicit dashed vertical lines — "every threshold drawn as
    an explicit reference line" (§9.3), never left implicit in a caption."""
    returns = np.asarray(returns, dtype=float)
    returns = returns[np.isfinite(returns)]
    if returns.size == 0:
        return _empty_svg(width, height)

    lo, hi = float(returns.min()), float(returns.max())
    if lo == hi:
        lo, hi = lo - 0.01, hi + 0.01
    counts, edges = np.histogram(returns, bins=bins, range=(lo, hi))
    max_count = max(int(counts.max()), 1)

    plot_x0 = _scale(edges[:-1], lo, hi, pad, width - pad)
    plot_x1 = _scale(edges[1:], lo, hi, pad, width - pad)
    bar_height = _scale(counts.astype(float), 0, max_count, 0, height - 2 * pad)

    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg">']
    for x0, x1, h in zip(plot_x0, plot_x1, bar_height):
        y = (height - pad) - h
        parts.append(f'<rect x="{x0:.1f}" y="{y:.1f}" width="{max(x1 - x0 - 1, 0.5):.1f}" height="{h:.1f}" fill="{_LINE}" opacity="0.75"/>')
    parts.append(f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="{_BORDER}"/>')

    for label, value in (reference_lines or {}).items():
        if value is None or not (lo <= value <= hi or True):
            continue
        x = float(_scale(np.array([value]), lo, hi, pad, width - pad)[0])
        x = max(pad, min(width - pad, x))
        parts.append(f'<line x1="{x:.1f}" y1="{pad}" x2="{x:.1f}" y2="{height - pad}" class="threshold-line"/>')
        parts.append(f'<text x="{x:.1f}" y="{pad - 4}" fill="{_MUTED}" text-anchor="middle">{label}</text>')

    parts.append(f'<text x="{pad}" y="{height - 6}" fill="{_MUTED}">{lo:.1%}</text>')
    parts.append(f'<text x="{width - pad}" y="{height - 6}" fill="{_MUTED}" text-anchor="end">{hi:.1%}</text>')
    parts.append("</svg>")
    return "".join(parts)


def regime_bars_svg(
    rows: Sequence[dict],
    *,
    key: str = "sharpe",
    width: int = 520,
    height: int = 180,
    pad_left: int = 90,
    pad: int = 16,
) -> str:
    """Performance by regime, small multiples (UI-UX-Brief §3.2④) — one
    horizontal bar per regime, `rows` items shaped like
    `RegimePerformanceRepository.for_evaluation` results (`regime`, `sharpe`,
    ...). Negative values extend left of a zero line, mirroring the
    underwater chart's own below-axis fill for "this went wrong here."
    """
    if not rows:
        return _empty_svg(width, height)

    values = [float(row.get(key) or 0.0) for row in rows]
    lo, hi = min(values + [0.0]), max(values + [0.0])
    if lo == hi:
        lo, hi = lo - 1.0, hi + 1.0
    row_height = (height - 2 * pad) / len(rows)
    zero_x = float(_scale(np.array([0.0]), lo, hi, pad_left, width - pad)[0])

    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'<line x1="{zero_x:.1f}" y1="{pad}" x2="{zero_x:.1f}" y2="{height - pad}" stroke="{_BORDER}"/>')
    for i, (row, value) in enumerate(zip(rows, values)):
        y = pad + i * row_height
        x = float(_scale(np.array([value]), lo, hi, pad_left, width - pad)[0])
        bar_x, bar_w = (zero_x, x - zero_x) if value >= 0 else (x, zero_x - x)
        fill = _LINE if value >= 0 else _NEG_FILL
        bar_h = row_height * 0.6
        parts.append(
            f'<rect x="{bar_x:.1f}" y="{y + row_height * 0.2:.1f}" width="{abs(bar_w):.1f}" '
            f'height="{bar_h:.1f}" fill="{fill}"/>'
        )
        label = str(row.get("regime", ""))
        parts.append(f'<text x="8" y="{y + row_height * 0.5 + 4:.1f}" fill="{_MUTED}">{label}</text>')
        parts.append(f'<text x="{width - 4}" y="{y + row_height * 0.5 + 4:.1f}" fill="{_MUTED}" text-anchor="end">{value:.2f}</text>')
    parts.append("</svg>")
    return "".join(parts)


def knowledge_graph_svg(edges: Sequence[dict], *, width: int = 640, height: int = 420) -> str:
    """The knowledge graph node-link view (UI-UX-Brief §7) — a plain
    circular layout, not a force-directed one: the knowledge graph is small
    (dozens, not thousands, of subject/object nodes) and a fixed layout
    needs no client-side simulation library to draw.

    `edges` are `knowledge_edges` rows (`subject`, `predicate`, `object`,
    `evidence_count`, `counter_evidence_count`). Edge thickness scales with
    `evidence_count` (§7: "edge thickness = evidence count"); an edge
    carrying counter-evidence draws in the against-colour rather than the
    link colour, so contradiction is visible in the picture itself, not only
    in the table beneath it.
    """
    if not edges:
        return _empty_svg(width, height, "no graph edges yet")

    nodes = sorted({edge["subject"] for edge in edges} | {edge["object"] for edge in edges})
    n = len(nodes)
    cx, cy = width / 2.0, height / 2.0
    radius = min(width, height) / 2.0 - 60.0
    positions = {}
    for i, node in enumerate(nodes):
        angle = 2.0 * math.pi * i / n if n else 0.0
        positions[node] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))

    max_evidence = max((edge.get("evidence_count") or 0) for edge in edges) or 1

    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg">']
    for edge in edges:
        subject, object_ = edge.get("subject"), edge.get("object")
        if subject not in positions or object_ not in positions:
            continue
        x1, y1 = positions[subject]
        x2, y2 = positions[object_]
        stroke_width = 1.0 + 4.0 * (edge.get("evidence_count") or 0) / max_evidence
        colour = _NEG_FILL if edge.get("counter_evidence_count") else _LINE
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{colour}" stroke-width="{stroke_width:.1f}" opacity="0.8"/>'
        )
    for node, (x, y) in positions.items():
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{_MUTED}"/>')
        parts.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" text-anchor="middle" fill="{_MUTED}">{escape(str(node))}</text>')
    parts.append("</svg>")
    return "".join(parts)

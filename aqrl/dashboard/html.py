"""HTML rendering primitives — the whole templating layer (Implementation_Plan
§15 / UI-UX-Brief §12: "server-rendered pages; minimal client JS").

No template engine: every page is an f-string built from these few helpers.
`escape()` is the one mandatory gate between a database value (a human's typed
note, an LLM's rationale, a strategy name) and the response body. Most
helpers here route their text arguments through it automatically —
`table`/`kv_table`/`badge`/`page`/`empty_state`/`confirm_form`. The one
exception is `case_against_panel`: its `items` are pre-rendered HTML
fragments (a view composes each from `badge()` output plus prose), so the
panel must not double-escape them — the **caller** escapes any raw value
before interpolating it into an item string.

Colour is fixed, never decorative (UI-UX-Brief §9.2): green/yellow/orange/red
mean *behaving as validated*, exclusively for health. `pnl_class` is a
deliberately separate, non-red/green scale — a losing strategy is not a red
strategy, and conflating the two is exactly the confusion the health
framework exists to prevent.
"""
from __future__ import annotations

from html import escape
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "badge",
    "bar",
    "case_against_panel",
    "confirm_form",
    "empty_state",
    "escape",
    "kv_table",
    "page",
    "pnl_class",
    "section",
    "table",
]

NAV_ITEMS = ("Decisions", "Health", "Pipeline", "Laboratory", "Knowledge")

#: Health semantics only (UI-UX-Brief §9.2) — never reused for profit/loss.
_HEALTH_COLOURS = {
    "green": "#2e7d32",
    "yellow": "#b8860b",
    "orange": "#c9660a",
    "red": "#c62828",
    "pass": "#2e7d32",
    "warn": "#b8860b",
    "fail": "#c62828",
    "low": "#2e7d32",
    "medium": "#b8860b",
    "high": "#c62828",
}

_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5f5f5f; --border: #d8d8d8;
  --panel: #f7f7f7; --against-bg: #fdf1f0; --against-border: #c62828;
  --link: #1a5fb4; --pnl-up: #1a5fb4; --pnl-down: #7a4b00;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #14161a; --fg: #e8e8e8; --muted: #9a9a9a; --border: #35383e;
          --panel: #1c1f24; --against-bg: #2a1a19; --against-border: #e57373;
          --link: #7fb2ff; --pnl-up: #7fb2ff; --pnl-down: #e0b168; }
}
:root[data-theme="dark"] { --bg: #14161a; --fg: #e8e8e8; --muted: #9a9a9a; --border: #35383e;
  --panel: #1c1f24; --against-bg: #2a1a19; --against-border: #e57373;
  --link: #7fb2ff; --pnl-up: #7fb2ff; --pnl-down: #e0b168; }
:root[data-theme="light"] { --bg: #ffffff; --fg: #1a1a1a; --muted: #5f5f5f; --border: #d8d8d8;
  --panel: #f7f7f7; --against-bg: #fdf1f0; --against-border: #c62828;
  --link: #1a5fb4; --pnl-up: #1a5fb4; --pnl-down: #7a4b00; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 15px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
nav { display: flex; gap: 0; border-bottom: 1px solid var(--border); padding: 0 1rem; }
nav a { padding: 0.9rem 1rem; color: var(--muted); text-decoration: none; font-weight: 600;
        font-size: 0.9rem; letter-spacing: 0.02em; border-bottom: 2px solid transparent; }
nav a.active { color: var(--fg); border-bottom-color: var(--fg); }
main { max-width: 980px; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
main.wide { max-width: 1180px; }
h1 { font-size: 1.4rem; margin: 0 0 1rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 0.6rem; }
.muted { color: var(--muted); }
.mono, table { font-variant-numeric: tabular-nums; }
table { border-collapse: collapse; width: 100%; margin: 0.5rem 0 1rem; overflow-x: auto; display: block; }
table thead { display: table; width: 100%; table-layout: fixed; }
table tbody { display: table; width: 100%; table-layout: fixed; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.03em; }
tr:hover td { background: var(--panel); }
a { color: var(--link); }
.card { border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.2rem; margin: 0.8rem 0; }
.against { background: var(--against-bg); border: 1px solid var(--against-border); border-radius: 8px;
           padding: 1rem 1.2rem; margin: 1.2rem 0; }
.against h2 { margin-top: 0; color: var(--against-border); }
.badge { display: inline-block; padding: 0.1rem 0.55rem; border-radius: 999px; font-size: 0.78rem;
         font-weight: 700; color: #fff; }
.warn-badge { display: inline-block; padding: 0.05rem 0.5rem; border-radius: 4px; font-size: 0.78rem;
              font-weight: 700; background: var(--against-border); color: #fff; }
.bar-row { display: flex; align-items: center; gap: 0.6rem; margin: 0.25rem 0; }
.bar-track { flex: 1; height: 0.6rem; background: var(--panel); border-radius: 4px; overflow: hidden;
             border: 1px solid var(--border); }
.bar-fill { height: 100%; }
kbd { border: 1px solid var(--border); border-radius: 3px; padding: 0 0.3rem; font-size: 0.85em; }
form.inline { display: inline; }
button, input[type=submit] { font: inherit; padding: 0.45rem 0.9rem; border-radius: 6px;
       border: 1px solid var(--border); background: var(--panel); color: var(--fg); cursor: pointer; }
button.primary { background: var(--fg); color: var(--bg); border-color: var(--fg); }
button.danger { border-color: var(--against-border); color: var(--against-border); }
textarea, input[type=text] { font: inherit; width: 100%; padding: 0.5rem; border-radius: 6px;
       border: 1px solid var(--border); background: var(--bg); color: var(--fg); }
.controls { display: flex; gap: 0.6rem; justify-content: center; margin: 1.5rem 0; flex-wrap: wrap; }
.empty { text-align: center; padding: 3rem 1rem; color: var(--muted); }
.pnl-up { color: var(--pnl-up); font-weight: 600; }
.pnl-down { color: var(--pnl-down); font-weight: 600; }
details > summary { cursor: pointer; font-weight: 600; margin: 1rem 0 0.3rem; }
svg text { fill: var(--fg); font-size: 10px; }
.threshold-line { stroke: var(--muted); stroke-dasharray: 3 3; }
"""

_KEYBOARD_JS = """
<script>
document.addEventListener('keydown', function (e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  var rows = document.querySelectorAll('[data-queue-row]');
  if (!rows.length) return;
  var idx = rows.findIndex(function (r) { return r.classList.contains('focused'); });
  if (e.key === 'j' || e.key === 'ArrowDown') {
    if (idx >= 0) rows[idx].classList.remove('focused');
    idx = Math.min(idx + 1, rows.length - 1);
    rows[idx].classList.add('focused'); rows[idx].scrollIntoView({block: 'nearest'});
  } else if (e.key === 'k' || e.key === 'ArrowUp') {
    if (idx >= 0) rows[idx].classList.remove('focused');
    idx = Math.max(idx - 1, 0);
    rows[idx].classList.add('focused'); rows[idx].scrollIntoView({block: 'nearest'});
  } else if (e.key === 'Enter' && idx >= 0) {
    var link = rows[idx].querySelector('a');
    if (link) window.location = link.href;
  }
});
</script>
"""


def page(title: str, body: str, *, active: str | None = None, wide: bool = False) -> str:
    """The one page shell every view wraps its body in."""
    nav = "".join(
        f'<a href="/{item.lower()}" class="{"active" if item == active else ""}">{item}</a>'
        for item in NAV_ITEMS
    )
    main_class = ' class="wide"' if wide else ""
    keyboard = _KEYBOARD_JS if active == "Decisions" else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(title)} — AQRL</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_CSS}</style></head>
<body>
<nav>{nav}</nav>
<main{main_class}>{body}</main>
{keyboard}
</body></html>"""


def section(title: str, body: str) -> str:
    return f"<h2>{escape(title)}</h2>\n{body}"


def badge(text: str, level: str) -> str:
    colour = _HEALTH_COLOURS.get(level, "#5f5f5f")
    return f'<span class="badge" style="background:{colour}">{escape(text)}</span>'


def pnl_class(value: float | None) -> str:
    """P&L's own non-red/green scale (UI-UX-Brief §9.2) — deliberately not
    `badge`/`_HEALTH_COLOURS`, so a losing trade never borrows health red."""
    if value is None:
        return "muted"
    return "pnl-up" if value >= 0 else "pnl-down"


def bar(value: float, maximum: float, *, level: str = "green", label: str | None = None) -> str:
    pct = 0.0 if maximum <= 0 else max(0.0, min(1.0, value / maximum)) * 100.0
    colour = _HEALTH_COLOURS.get(level, "#5f5f5f")
    text = escape(label) if label is not None else f"{value:.2f}"
    return (
        '<div class="bar-row"><div class="bar-track">'
        f'<div class="bar-fill" style="width:{pct:.1f}%;background:{colour}"></div>'
        f'</div><span class="mono">{text}</span></div>'
    )


def table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str], *, headers: Sequence[str] | None = None) -> str:
    if not rows:
        return '<p class="muted">none</p>'
    heads = headers or columns
    head_html = "".join(f"<th>{escape(str(h))}</th>" for h in heads)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{escape('' if row.get(c) is None else str(row.get(c)))}</td>" for c in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head_html}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def kv_table(pairs: Iterable[tuple[str, Any]]) -> str:
    rows = "".join(
        f'<tr><td class="muted">{escape(str(k))}</td><td>{escape("" if v is None else str(v))}</td></tr>'
        for k, v in pairs
    )
    return f"<table><tbody>{rows}</tbody></table>"


def empty_state(message: str) -> str:
    return f'<div class="empty">{escape(message)}</div>'


def case_against_panel(title: str, items: Sequence[str]) -> str:
    """UI-UX-Brief §3.2 ②: the case against, above the case for, in a
    red-bordered panel — placed here so every view module builds it the same
    way rather than hand-rolling the border colour per screen.

    `items` are pre-rendered HTML fragments (typically `badge(...)` plus
    escaped prose), not raw text — **the caller must `escape()` any raw
    value before composing an item**, the same way `f"{badge(...)} some
    text"` composes elsewhere in this module."""
    if not items:
        body = '<p class="muted">no material objections found</p>'
    else:
        body = "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"
    return f'<div class="against"><h2>{escape(title)}</h2>{body}</div>'


def confirm_form(
    action: str,
    *,
    fields: Sequence[tuple[str, str, str]] = (),
    submit_label: str = "Submit",
    danger: bool = False,
    require_typed: str | None = None,
) -> str:
    """A POST form. `require_typed` renders a plain-text hint asking the
    operator to type it into the last text field — real enforcement happens
    server-side (`server.py`'s handlers reject a mismatched confirmation),
    this is only the UI half of UI-UX-Brief §10.1's "typed confirmation."
    `fields` is `(name, label, kind)`, kind in {"text", "textarea"}.
    """
    field_html = []
    for name, label, kind in fields:
        input_html = (
            f'<textarea name="{escape(name)}" rows="3"></textarea>'
            if kind == "textarea"
            else f'<input type="text" name="{escape(name)}">'
        )
        field_html.append(f'<label>{escape(label)}<br>{input_html}</label>')
    hint = f'<p class="muted">Type <kbd>{escape(require_typed)}</kbd> to confirm.</p>' if require_typed else ""
    button_class = "danger" if danger else "primary"
    return (
        f'<form method="post" action="{escape(action)}">{"".join(field_html)}{hint}'
        f'<button type="submit" class="{button_class}">{escape(submit_label)}</button></form>'
    )

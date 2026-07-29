"""P0 — smoke, correctness, look-ahead and leakage.

**The most underrated phase** (TRD §10.1). Look-ahead bias and data leakage are
the dominant failure modes of generated strategy code, and a strategy that fails
P0 is a **bug**: it routes back to A2 with the diagnostic and is never recorded
as a research finding, because a leaked result is not evidence about markets.

Four kinds of check live here, and they catch different things:

1. **Static** — an AST scan of strategy *source*. Catches `shift(-n)`,
   `bfill()`, centred windows, whole-series statistics. Cheap, and blind to
   anything expressed through a library call.
2. **Empirical** — truncation invariance. A signal computed with data up to bar
   `t` must be identical whether or not the strategy was shown bars after `t`.
   Catches leaks the AST cannot see, e.g. `df['close'].iloc[-1]`.
3. **Structural** — properties of the spec DAG, notably absolute price levels
   (§14.2c: back-adjusting makes an absolute level forward-looking, since the
   factor applied to 2009's prices depends on a split announced in 2019).
4. **Provenance** — properties of the *data*: corporate-action adjusted,
   point-in-time resolved, validated, not from the vault. TRD §10.1 requires
   these to be **rejected outright**, not warned about.

Both scanners keep their branches explicit rather than collapsing them into
precedence-dependent boolean chains. This is the one place in the codebase where
a subtly misread condition silently stops catching leaks, so readability wins.
"""
from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from ..operators.spec import SOURCE_PREFIX, StrategySpec
from ..profiles.models import MarketProfile
from .checks import CheckResult, verdict

__all__ = [
    "SignalFn",
    "empirical_leakage_scan",
    "empirical_leakage_scan_arrays",
    "run_p0",
    "spec_absolute_level_scan",
    "static_lookahead_scan",
]

SignalFn = Callable[[pd.DataFrame, dict], pd.Series]

_FORBIDDEN_CALL_NAMES = {"bfill", "backfill"}
_UNROLLED_STAT_METHODS = {"mean", "std", "var", "zscore", "rank"}

#: Raw price columns. A constant compared against one of these is an absolute
#: price level, whatever the spec calls it.
_RAW_PRICE_SOURCES = {"open", "high", "low", "close"}

#: Operators whose output is unit-free, so a constant threshold on them is a
#: statement about shape rather than about price. Anything not on this list is
#: assumed to carry price units — the conservative direction.
_UNIT_FREE_OPERATORS = frozenset(
    {"zscore", "percent_rank", "roc", "log_return", "frac_diff", "atr_normalise"}
)

#: Operators that compare a series against a constant parameter.
_LEVEL_OPERATORS = frozenset({"threshold"})


# ---------------------------------------------------------------------------
# P0a — static look-ahead scan (AST-based, TRD §9.4 / §10.1)
# ---------------------------------------------------------------------------


class _LookaheadVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id

        if name in _FORBIDDEN_CALL_NAMES:
            self.violations.append(
                f"line {node.lineno}: forbidden call `{name}()` (pulls the future backwards)"
            )

        if name == "shift" and node.args:
            arg = node.args[0]
            # Kept as two explicit branches. Collapsing them into one
            # `A and B or C and D` chain relies on operator precedence, and this
            # is the look-ahead scanner — the one place in the codebase where a
            # subtly misread condition silently stops catching leaks.
            if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                self.violations.append(f"line {node.lineno}: `shift(-n)` is forbidden anywhere")
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)) and arg.value < 0:
                self.violations.append(f"line {node.lineno}: `shift(-n)` is forbidden anywhere")

        if name == "fillna":
            for kw in node.keywords:
                if kw.arg == "method" and isinstance(kw.value, ast.Constant) and kw.value.value in (
                    "backfill",
                    "bfill",
                ):
                    self.violations.append(
                        f"line {node.lineno}: `fillna(method='{kw.value.value}')` is forbidden"
                    )

        if name in ("rolling", "expanding", "ewm"):
            for kw in node.keywords:
                if kw.arg == "center" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    self.violations.append(
                        f"line {node.lineno}: centred windows (`center=True`) are forbidden"
                    )

        # Whole-series stats: `.mean()/.std()/...` NOT chained off `.rolling(/.expanding(/.ewm(`.
        if name in _UNROLLED_STAT_METHODS and isinstance(func, ast.Attribute):
            receiver = func.value
            chained_on_window = isinstance(receiver, ast.Call) and (
                isinstance(receiver.func, ast.Attribute)
                and receiver.func.attr in ("rolling", "expanding", "ewm")
            )
            if not chained_on_window:
                self.violations.append(
                    f"line {node.lineno}: whole-series `.{name}()` — rolling statistics only, "
                    "never over the full series"
                )

        self.generic_visit(node)


def static_lookahead_scan(source: str) -> list[str]:
    """P0a. Returns a list of violation strings; empty means the scan passed."""
    tree = ast.parse(source)
    visitor = _LookaheadVisitor()
    visitor.visit(tree)
    return visitor.violations


# ---------------------------------------------------------------------------
# P0b — empirical leakage scan: truncation invariance
# ---------------------------------------------------------------------------


def empirical_leakage_scan_arrays(
    signal_fn: Callable[[dict[str, np.ndarray]], np.ndarray],
    columns: Mapping[str, np.ndarray],
    checkpoints: tuple[float, ...] = (0.5, 0.7, 0.85),
    tolerance: float = 1e-9,
    label: str = "",
) -> list[str]:
    """P0b, on plain arrays.

    A signal computed with only data up to bar `t` must be identical whether or
    not the strategy was ever shown bars after `t`. Truncate the input at
    several checkpoints and compare against the full-sample signal over the
    overlapping region — any mismatch means the future leaked backwards
    somewhere the static scan could not see.
    """
    violations: list[str] = []
    full_signal = np.asarray(signal_fn(dict(columns)), dtype=float)
    n = len(next(iter(columns.values()))) if columns else 0

    for fraction in checkpoints:
        k = int(n * fraction)
        if k < 10:
            continue
        truncated = np.asarray(
            signal_fn({name: values[:k] for name, values in columns.items()}), dtype=float
        )
        overlap = min(truncated.size, k)
        # Ignore the tail of the truncated window — a rolling window's most
        # recent points can legitimately differ once enough of it is missing
        # relative to the warm-up; compare only the stable prefix.
        stable = max(overlap - 5, 0)
        if stable == 0:
            continue
        left, right = full_signal[:stable], truncated[:stable]
        mask = ~(np.isnan(left) & np.isnan(right))
        if not np.allclose(np.nan_to_num(left[mask]), np.nan_to_num(right[mask]), atol=tolerance):
            where = f" [{label}]" if label else ""
            violations.append(
                f"truncation at {fraction:.0%} of history changed earlier signal values"
                f"{where} — future data leaked backwards"
            )
    return violations


def empirical_leakage_scan(
    df: pd.DataFrame,
    generate_signals: SignalFn,
    params: dict,
    checkpoints: tuple[float, ...] = (0.5, 0.7, 0.85),
    tolerance: float = 1e-9,
) -> list[str]:
    """P0b for a pandas strategy function — nanoAQRL's calling convention."""
    violations: list[str] = []
    full_signal = generate_signals(df, params)
    n = len(df)

    for fraction in checkpoints:
        k = int(n * fraction)
        if k < 10:
            continue
        truncated = generate_signals(df.iloc[:k], params)
        overlap = min(len(truncated), k)
        stable = max(overlap - 5, 0)
        if stable == 0:
            continue
        left = full_signal.iloc[:stable].to_numpy(dtype=float)
        right = truncated.iloc[:stable].to_numpy(dtype=float)
        mask = ~(np.isnan(left) & np.isnan(right))
        if not np.allclose(np.nan_to_num(left[mask]), np.nan_to_num(right[mask]), atol=tolerance):
            violations.append(
                f"truncation at {fraction:.0%} of history changed earlier signal values "
                "— future data leaked backwards"
            )
    return violations


# ---------------------------------------------------------------------------
# P0c — structural: absolute price levels
# ---------------------------------------------------------------------------


def spec_absolute_level_scan(spec: StrategySpec) -> list[str]:
    """Flag constant thresholds compared against a price-scaled series.

    *"No absolute price thresholds — back-adjusted series make absolute levels
    forward-looking"* (TRD §10.1, §14.2c). `close > 500` is not a rule about the
    market: after back-adjustment the number 500 refers to a different level in
    2009 than it did before the 2019 split was applied, so the rule's meaning
    depends on corporate actions that had not happened yet.

    **The heuristic, stated plainly.** A `threshold` node is flagged when its
    upstream cone reaches a raw price column without passing through any
    unit-free transform. It therefore accepts `threshold(zscore(close))` and
    rejects `threshold(rolling_mean(close))` — the latter is still in rupees.
    A strategy could still smuggle an absolute level through an operator this
    list does not know about; the empirical scan does not catch that either, and
    it is named here rather than left as an assumed guarantee.
    """
    nodes = spec.nodes()
    violations: list[str] = []

    def cone(node_id: str, seen: set[str]) -> tuple[set[str], set[str]]:
        """(operator names, raw price sources) reachable upstream of a node."""
        if node_id in seen:
            return set(), set()
        seen.add(node_id)
        node = nodes[node_id]
        operators, sources = {node.operator}, set()
        for reference in node.inputs.values():
            if reference.startswith(SOURCE_PREFIX):
                sources.add(reference[len(SOURCE_PREFIX) :])
            else:
                upstream_operators, upstream_sources = cone(reference, seen)
                operators |= upstream_operators
                sources |= upstream_sources
        return operators, sources

    for node_id, node in nodes.items():
        if node.operator not in _LEVEL_OPERATORS:
            continue
        operators, sources = cone(node_id, set())
        if not (sources & _RAW_PRICE_SOURCES):
            continue
        if operators & _UNIT_FREE_OPERATORS:
            continue
        violations.append(
            f"node {node_id!r} compares a price-scaled series against the constant level(s) "
            f"{sorted(k for k in node.params if k in ('upper', 'lower')) or ['upper', 'lower']} — "
            "absolute price levels are forward-looking on a back-adjusted series (TRD §14.2c)"
        )
    return violations


# ---------------------------------------------------------------------------
# P0d — provenance of the data itself
# ---------------------------------------------------------------------------


def snapshot_provenance_checks(
    snapshot: Mapping[str, Any], market: MarketProfile
) -> list[CheckResult]:
    """The data-side half of P0. Every one of these is a rejection, not a warning."""
    checks = [
        verdict(
            snapshot.get("validation_status") == "valid",
            "snapshot_validated",
            "correctness",
            detail=f"validation_status={snapshot.get('validation_status')!r}",
        ),
        verdict(
            not snapshot.get("in_vault", 0),
            "snapshot_outside_vault",
            "correctness",
            detail="the research loop has no read path to vault data (TRD §15.2)",
        ),
        verdict(
            bool(snapshot.get("adjusted", 0)) and snapshot.get("adjustment_method") != "none",
            "corporate_actions_adjusted",
            "correctness",
            detail=(
                f"adjustment_method={snapshot.get('adjustment_method')!r}; an unadjusted series "
                "reads a 1:2 split as a −50% move and corrupts every price-based indicator"
            ),
        ),
    ]

    # Point-in-time membership is only meaningful where the universe is an
    # index. A market trading an explicit instrument list has no membership to
    # resolve, and demanding the flag there would be theatre.
    if market.universe.index_name:
        checks.append(
            verdict(
                bool(snapshot.get("point_in_time_membership", 0)),
                "point_in_time_universe",
                "correctness",
                detail=(
                    f"{market.universe.index_name} membership must be resolved as of each bar; "
                    "a static universe projects today's index back through history (TRD §14.3)"
                ),
            )
        )
    return checks


# ---------------------------------------------------------------------------
# The phase
# ---------------------------------------------------------------------------


def run_p0(
    spec: StrategySpec,
    signal_fn: Callable[[dict[str, np.ndarray]], np.ndarray],
    columns_by_instrument: list[tuple[str, dict[str, np.ndarray]]],
    snapshot: Mapping[str, Any],
    market: MarketProfile,
    source: str | None = None,
) -> list[CheckResult]:
    """Run every P0 check and return one `CheckResult` per test.

    `source` is optional because a spec-compiled strategy has no Python source
    of its own — its causality is guaranteed at the operator boundary instead,
    by Stage 2's registry-wide truncation-invariance suite. When A2 starts
    writing free-form code (Stage 5), the source arrives and the static scan
    becomes load-bearing again.
    """
    checks: list[CheckResult] = []

    if source is not None:
        violations = static_lookahead_scan(source)
        checks.append(
            verdict(
                not violations,
                "static_lookahead_scan",
                "correctness",
                value=float(len(violations)),
                threshold=0.0,
                detail="; ".join(violations) or None,
            )
        )

    level_violations = spec_absolute_level_scan(spec)
    checks.append(
        verdict(
            not level_violations,
            "no_absolute_price_levels",
            "correctness",
            value=float(len(level_violations)),
            threshold=0.0,
            detail="; ".join(level_violations) or None,
        )
    )

    leaks: list[str] = []
    for instrument, columns in columns_by_instrument:
        leaks.extend(
            empirical_leakage_scan_arrays(signal_fn, columns, label=instrument)
        )
    checks.append(
        verdict(
            not leaks,
            "truncation_invariance",
            "correctness",
            value=float(len(leaks)),
            threshold=0.0,
            detail="; ".join(leaks[:5]) or None,
        )
    )

    finite = all(
        np.isfinite(np.asarray(signal_fn(columns), dtype=float)).all()
        for _, columns in columns_by_instrument
    )
    checks.append(
        verdict(
            finite,
            "signals_finite",
            "correctness",
            detail="NaN or inf in the signal series" if not finite else None,
        )
    )

    checks.extend(snapshot_provenance_checks(snapshot, market))
    return checks

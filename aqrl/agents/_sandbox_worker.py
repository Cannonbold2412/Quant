"""The child process `sandbox.py` spawns — the actual static-check pipeline.

Runs with a scrubbed environment, resource limits, and a wall-clock timeout
imposed by the parent (TRD §18: *"A2's code runs isolated, with no network and
no credentials"*). This module assumes nothing about its surroundings beyond
"a Python interpreter that can import `aqrl`" — no database, no settings, no
secrets — and communicates with the parent purely over stdin/stdout JSON, so
a crash here is a parse failure the parent handles, never a hang.

Every check below mirrors a phase of Stage 3's P0 (`aqrl/eval/p0.py`), reused
rather than reimplemented (TRD §6.1's no-forking rule applies to checks, not
just the engine itself): the static look-ahead scan, the absolute-price-level
scan, and the empirical truncation-invariance scan are the exact functions P0
runs during `EVALUATE` — Stage 5 just runs them earlier, on a synthetic smoke
frame, so a structurally broken spec never reaches a real evaluation slot.
"""
from __future__ import annotations

import ast
import json
import sys

import numpy as np

from ..eval.checks import CheckResult, verdict
from ..eval.p0 import empirical_leakage_scan, spec_absolute_level_scan, static_lookahead_scan
from ..operators.compile import compile_spec
from ..operators.spec import StrategySpec
from ..profiles.models import ResolvedProfile

__all__ = ["main", "run_checks"]


def _smoke_frame(n_days: int, seed: int = 0):
    """A small synthetic OHLCV panel — enough to exercise every node once.

    Not real data, and not meant to be: this only asks "does the rendered
    module's `generate_signals` run, stay finite, and stay causal under
    truncation?", the same question P0 asks, at a fraction of the size.
    """
    from nanoaqrl._lib.synthetic_data import synthetic_ohlcv

    return synthetic_ohlcv(n_days, seed=seed)


def run_checks(payload: dict) -> list[dict]:
    checks: list[CheckResult] = []
    source: str = payload["source"]
    expected_hash: str = payload["spec_hash"]

    try:
        compiled_code = compile(source, "<generated-strategy>", "exec")
        ast.parse(source)
        checks.append(verdict(True, "compiles", "correctness"))
    except SyntaxError as exc:
        checks.append(verdict(False, "compiles", "correctness", detail=str(exc)))
        return [c.row() for c in checks]

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

    namespace: dict = {}
    try:
        exec(compiled_code, namespace)  # noqa: S102 - executing generated code is this module's job
        spec: StrategySpec = namespace["SPEC"]
        generate_signals = namespace["generate_signals"]
    except Exception as exc:  # noqa: BLE001 - any import-time failure is itself a finding
        checks.append(verdict(False, "imports_cleanly", "correctness", detail=str(exc)))
        return [c.row() for c in checks]
    checks.append(verdict(True, "imports_cleanly", "correctness"))

    actual_hash = spec.spec_hash()
    matches = actual_hash == expected_hash
    checks.append(
        verdict(
            matches,
            "spec_hash_matches_stored_row",
            "correctness",
            detail=None if matches else f"rendered module hashes to {actual_hash}, expected {expected_hash}",
        )
    )

    try:
        spec.validate_spec()
    except Exception as exc:  # noqa: BLE001
        checks.append(verdict(False, "spec_validates", "correctness", detail=str(exc)))
        return [c.row() for c in checks]
    checks.append(verdict(True, "spec_validates", "correctness"))

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

    resolved = ResolvedProfile.model_validate(payload["resolved_profile"])
    try:
        compile_spec(spec, resolved)  # side effect: raises on a market/timeframe the spec cannot run under
    except Exception as exc:  # noqa: BLE001
        checks.append(verdict(False, "operator_applicability", "correctness", detail=str(exc)))
        return [c.row() for c in checks]
    checks.append(verdict(True, "operator_applicability", "correctness"))

    try:
        frame = _smoke_frame(int(payload.get("smoke_days", 260)))
        signal = generate_signals(frame, {}).to_numpy(dtype=float)
        finite = bool(np.isfinite(signal).all())
        in_range = bool(np.all((signal >= -1.0) & (signal <= 1.0)))
    except Exception as exc:  # noqa: BLE001
        checks.append(verdict(False, "smoke_run_produces_finite_signal", "correctness", detail=str(exc)))
        return [c.row() for c in checks]
    checks.append(
        verdict(
            finite and in_range,
            "smoke_run_produces_finite_signal",
            "correctness",
            detail=None if finite and in_range else "signal contained NaN/inf, or left [-1, 1]",
        )
    )

    leaks = empirical_leakage_scan(frame, generate_signals, {})
    checks.append(
        verdict(
            not leaks,
            "truncation_invariance",
            "correctness",
            value=float(len(leaks)),
            threshold=0.0,
            detail="; ".join(leaks) or None,
        )
    )

    return [c.row() for c in checks]


def main() -> None:
    payload = json.loads(sys.stdin.read())
    try:
        rows = run_checks(payload)
        json.dump({"checks": rows, "error": None}, sys.stdout)
    except Exception as exc:  # noqa: BLE001 - the sandbox must always answer, never crash silently
        json.dump({"checks": [], "error": f"{type(exc).__name__}: {exc}"}, sys.stdout)


if __name__ == "__main__":
    main()

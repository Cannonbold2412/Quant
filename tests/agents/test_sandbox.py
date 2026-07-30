"""Sandboxed static checks (TRD §18): every finding a spec can produce, plus
the isolation guarantees — no credentials, no unbounded runtime.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from aqrl.agents.render import render_module
from aqrl.agents.sandbox import SandboxTimeout, run_sandboxed_checks
from aqrl.operators.spec import StrategySpec
from aqrl.profiles import ProfileLoader

from .conftest import absolute_level_spec_json


@pytest.fixture
def resolved():
    return ProfileLoader().resolve("nse_equity", "daily", "cash_equity")


def _checks_by_name(checks):
    return {c.test_name: c for c in checks}


def test_a_clean_spec_passes_every_check(crossover_spec, resolved):
    rendered = render_module(crossover_spec, strategy_uid="uid", spec_version=1)
    checks = run_sandboxed_checks(rendered.source, spec_hash=crossover_spec.spec_hash(), resolved=resolved)
    by_name = _checks_by_name(checks)
    assert by_name  # non-empty
    assert all(c.result == "pass" for c in checks), [c.row() for c in checks if c.result != "pass"]


def test_absolute_price_level_is_caught(resolved):
    spec = StrategySpec(**absolute_level_spec_json())
    rendered = render_module(spec, strategy_uid="uid", spec_version=1)
    checks = run_sandboxed_checks(rendered.source, spec_hash=spec.spec_hash(), resolved=resolved)
    by_name = _checks_by_name(checks)
    assert by_name["no_absolute_price_levels"].result == "fail"
    # Everything upstream of the violation (compile, hash match) still passes —
    # this is a structural finding, not a crash.
    assert by_name["spec_hash_matches_stored_row"].result == "pass"


def test_spec_hash_mismatch_is_caught(crossover_spec, resolved):
    rendered = render_module(crossover_spec, strategy_uid="uid", spec_version=1)
    checks = run_sandboxed_checks(rendered.source, spec_hash="0" * 64, resolved=resolved)
    by_name = _checks_by_name(checks)
    assert by_name["spec_hash_matches_stored_row"].result == "fail"


def test_syntax_error_is_caught(resolved):
    checks = run_sandboxed_checks("def broken(:\n  pass", spec_hash="x", resolved=resolved)
    by_name = _checks_by_name(checks)
    assert by_name["compiles"].result == "fail"
    # Nothing past the syntax error was safe to attempt.
    assert len(checks) == 1


def test_deliberate_lookahead_is_caught(resolved):
    """A hand-injected `shift(-1)` in the rendered source — the AST scanner's
    job, exercised through the sandbox rather than calling it directly."""
    spec = StrategySpec(
        entry_logic=[{"id": "e", "operator": "ema", "params": {"span": 5}, "inputs": {"series": "price.close"}}],
        hypothesis="x",
    )
    rendered = render_module(spec, strategy_uid="uid", spec_version=1)
    tampered = rendered.source + (
        "\n_ORIGINAL = generate_signals\n"
        "def generate_signals(df, params):\n"
        "    return _ORIGINAL(df, params).shift(-1)\n"
    )
    checks = run_sandboxed_checks(tampered, spec_hash=spec.spec_hash(), resolved=resolved)
    by_name = _checks_by_name(checks)
    assert by_name["static_lookahead_scan"].result == "fail"


def test_operator_applicability_is_caught():
    """A spec using an operator that declares itself invalid for this market
    must fail applicability, not crash.

    No shipped operator currently declares a market restriction (the
    registry-wide grep for `valid_markets=` outside `base.py` comes up
    empty), so this registers a test-only one and exercises
    `_sandbox_worker.run_checks` directly, in-process — the subprocess
    boundary is what the other tests in this file exist to prove, not
    something this one needs to re-cross to test one `try/except` branch a
    dynamically-registered operator can't reach across a process boundary.
    """
    from aqrl.agents._sandbox_worker import run_checks
    from aqrl.operators.base import Operator
    from aqrl.operators.registry import register

    @register
    class _MarketRestrictedOperator(Operator):
        name = "sandbox_test_restricted_op"
        version = "1.0.0"
        category = "signal"
        inputs = ("series",)
        valid_markets = frozenset({"a_market_that_does_not_exist"})

        def apply(self, inputs, **params):
            return inputs["series"]

    spec = StrategySpec(
        entry_logic=[
            {"id": "e", "operator": "sandbox_test_restricted_op", "inputs": {"series": "price.close"}}
        ],
        hypothesis="x",
    )
    rendered = render_module(spec, strategy_uid="uid", spec_version=1)
    resolved = ProfileLoader().resolve("nse_equity", "daily", "cash_equity")
    payload = {
        "source": rendered.source,
        "spec_hash": spec.spec_hash(),
        "resolved_profile": resolved.model_dump(mode="json"),
        "smoke_days": 260,
    }
    checks = run_checks(payload)
    by_name = {row["test_name"]: row for row in checks}
    assert by_name["operator_applicability"]["result"] == "fail"


def test_sandbox_scrubs_credentials(crossover_spec, resolved, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("AQRL_DB_PATH", "/should/not/be/visible.db")
    rendered = render_module(crossover_spec, strategy_uid="uid", spec_version=1)
    # Tamper the source to try to read back an env var and fail the run if
    # the secret leaked through — a check the sandbox module doesn't define,
    # injected here purely to prove the environment really is scrubbed.
    tampered = rendered.source + (
        "\nimport os as _os\n"
        "assert 'ANTHROPIC_API_KEY' not in _os.environ, 'credential leaked into sandbox'\n"
        "assert 'AQRL_DB_PATH' not in _os.environ, 'db path leaked into sandbox'\n"
    )
    checks = run_sandboxed_checks(tampered, spec_hash=crossover_spec.spec_hash(), resolved=resolved)
    by_name = _checks_by_name(checks)
    assert by_name["imports_cleanly"].result == "pass"


def test_sandbox_enforces_a_wall_clock_timeout(crossover_spec, resolved, monkeypatch):
    monkeypatch.setenv("AQRL_SANDBOX_TIMEOUT_SECONDS", "1")
    from aqrl.config import reset_settings_cache

    reset_settings_cache()
    rendered = render_module(crossover_spec, strategy_uid="uid", spec_version=1)
    hung = rendered.source + "\nwhile True:\n    pass\n"
    with pytest.raises(SandboxTimeout):
        run_sandboxed_checks(hung, spec_hash=crossover_spec.spec_hash(), resolved=resolved)
    reset_settings_cache()


def test_sandbox_does_not_leave_a_process_running_after_timeout(crossover_spec, resolved, monkeypatch):
    """`subprocess.run(timeout=...)` kills the child on `TimeoutExpired` —
    confirm no orphaned sandbox worker survives the call."""
    monkeypatch.setenv("AQRL_SANDBOX_TIMEOUT_SECONDS", "1")
    from aqrl.config import reset_settings_cache

    reset_settings_cache()
    rendered = render_module(crossover_spec, strategy_uid="uid", spec_version=1)
    hung = rendered.source + "\nwhile True:\n    pass\n"
    try:
        run_sandboxed_checks(hung, spec_hash=crossover_spec.spec_hash(), resolved=resolved)
    except SandboxTimeout:
        pass
    reset_settings_cache()

    result = subprocess.run(
        ["pgrep", "-f", "aqrl.agents._sandbox_worker"], capture_output=True, text=True
    )
    survivors = [pid for pid in result.stdout.split() if pid != str(os.getpid())]
    assert not survivors, f"sandbox worker(s) still running after timeout: {survivors}"

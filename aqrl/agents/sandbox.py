"""Sandboxed execution of the static-check pipeline (TRD §18).

**No network, no credentials.** Every check that must actually run generated
code — importing the rendered module, compiling the spec against the market's
operator applicability rules, and the synthetic smoke run — happens in a
subprocess whose environment carries no `ANTHROPIC_*` key and no `AQRL_*`
database/data-root settings, with CPU and address-space limits and a
wall-clock timeout the parent enforces from outside.

**What this honestly provides, and what it does not.** This is process-level
isolation (`subprocess` + `resource` limits), not container-level (TRD §19
defers Docker to later). It stops generated code from reading this process's
credentials or environment, and from running away with CPU or memory. It does
**not** stop the subprocess from opening a socket — that requires a network
namespace or a seccomp filter, neither of which is in scope yet. Said plainly
here rather than implied by the name "sandbox".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from ..config import get_settings
from ..eval.checks import CheckResult
from ..profiles.models import ResolvedProfile

__all__ = ["SandboxError", "SandboxTimeout", "run_sandboxed_checks"]


class SandboxError(RuntimeError):
    """The sandbox process itself failed (crashed, or produced no result)."""


class SandboxTimeout(SandboxError):
    """The sandbox exceeded its wall-clock budget — treated as a check failure,
    not a job-level exception: a spec that hangs is a finding, not an outage."""


def _preexec_fn(cpu_seconds: int, memory_bytes: int):
    def _apply() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))

    return _apply


def run_sandboxed_checks(
    source: str,
    *,
    spec_hash: str,
    resolved: ResolvedProfile,
    smoke_days: int = 260,
) -> list[CheckResult]:
    """Run every static check against `source` in an isolated subprocess.

    Never raises on a *finding* — a compile error, a hash mismatch, a look-
    ahead violation all come back as a failing `CheckResult`, exactly like
    Stage 3's `run_p0`. It raises only when the sandbox itself could not
    produce an answer at all (timeout, crash, malformed output) — those are
    infrastructure failures, not research findings, and callers should treat
    them as a deterministic job failure (`FIX_CODE` still applies: the next
    attempt gets a fresh subprocess).
    """
    settings = get_settings()
    payload = {
        "source": source,
        "spec_hash": spec_hash,
        "resolved_profile": resolved.model_dump(mode="json"),
        "smoke_days": smoke_days,
    }

    # Deliberately minimal: no ANTHROPIC_*, no AQRL_* (no db_path/data_root),
    # so generated code has no path to credentials or the live database even
    # if it tried. PATH is kept so the interpreter itself can still resolve
    # its own shared libraries and any subprocess it might (legitimately or
    # not) try to spawn. The single-threaded BLAS/OMP pins are load-bearing,
    # not tidiness: NumPy's BLAS backend spawns a worker thread per core by
    # default, and each thread needs its own stack inside RLIMIT_AS — under a
    # tight address-space cap that thread creation itself fails first,
    # turning a memory limit on generated code into a crash in NumPy's import.
    env = {
        "PATH": os.environ.get("PATH", ""),
        # platform.machine() reads this on Windows; without it polars' CPU
        # capability probe silently disables CPUID and rejects its own
        # required feature flags at import.
        "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }

    preexec_fn = (
        _preexec_fn(settings.sandbox_cpu_seconds, settings.sandbox_memory_bytes)
        if os.name == "posix"
        else None
    )

    try:
        result = subprocess.run(
            [sys.executable, "-m", "aqrl.agents._sandbox_worker"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=settings.sandbox_timeout_seconds,
            preexec_fn=preexec_fn,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxTimeout(
            f"static-check sandbox exceeded its {settings.sandbox_timeout_seconds}s budget"
        ) from exc

    if result.returncode != 0:
        raise SandboxError(
            f"static-check sandbox exited {result.returncode}: {result.stderr[-4000:]}"
        )

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SandboxError(
            f"static-check sandbox produced non-JSON output: {result.stdout[-1000:]!r}"
        ) from exc

    if parsed.get("error"):
        raise SandboxError(f"static-check sandbox raised: {parsed['error']}")

    return [CheckResult(**row) for row in parsed["checks"]]

"""The engine's identity — stamped on every experiment (TRD §6.6).

A hand-maintained version string drifts. Somebody fixes a Sharpe edge case,
forgets to bump the constant, and forty thousand stored results silently claim
comparability with a scorer that no longer exists — which is precisely the rot
§6.6 exists to prevent.

So the stamp is a pair: a **semver** that a human bumps to declare intent, and a
**content hash** over this package's own sources that no one can forget. Stage 2
does the same for `operator_library_version`, for the same reason.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..hashing import hash_files

#: Bump on any change that alters a score. The hash catches the rest.
EVAL_ENGINE_SEMVER = "1.0.0"

_PACKAGE_ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def engine_content_hash() -> str:
    """A content hash over every `.py` in `aqrl/eval/`, order-independent."""
    sources = sorted(_PACKAGE_ROOT.rglob("*.py"))
    return hash_files(sources, _PACKAGE_ROOT)[:16]


@lru_cache(maxsize=1)
def engine_version() -> str:
    """`aqrl-eval-<semver>+<content hash>` — what lands in the database."""
    return f"aqrl-eval-{EVAL_ENGINE_SEMVER}+{engine_content_hash()}"


#: Module-level convenience for callers that want the string, not the call.
EVAL_ENGINE_VERSION = engine_version()

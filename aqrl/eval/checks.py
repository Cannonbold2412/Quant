"""One shape for every test the engine runs.

TRD §10.3: *"each phase yields `PASS | FAIL | WARN` per test plus a numeric
value **and its threshold**."* Both, always — a passing test with an invisible
threshold is not evidence, it is a reassuring noise. `evaluation_tests` in the
schema has exactly these columns, so a `CheckResult` is one row.

`gating` separates the two kinds of failure. A gating check that fails ends the
experiment; a non-gating one is recorded and the funnel continues. Breadth and
complexity ship non-gating on purpose — their definitions are open questions
owned by a human (`Implementation_Plan.md` §21), and inventing a threshold to
fill the gap would be the engine quietly setting policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "Category",
    "CheckLog",
    "CheckResult",
    "Outcome",
    "failed",
    "failures",
    "passed",
    "verdict",
    "warned",
    "worst",
]

Outcome = Literal["pass", "fail", "warn"]
Category = Literal["correctness", "performance", "robustness", "cost", "regime"]


@dataclass(frozen=True)
class CheckResult:
    """One row of `evaluation_tests`."""

    test_name: str
    category: Category
    result: Outcome
    gating: bool = True
    value: float | None = None
    threshold: float | None = None
    detail: str | None = None

    @property
    def blocking(self) -> bool:
        return self.gating and self.result == "fail"

    def row(self) -> dict:
        return {
            "test_name": self.test_name,
            "category": self.category,
            "result": self.result,
            "gating": self.gating,
            "value": self.value,
            "threshold": self.threshold,
            "detail": self.detail,
        }


def passed(name: str, category: Category, **fields) -> CheckResult:
    return CheckResult(name, category, "pass", **fields)


def failed(name: str, category: Category, **fields) -> CheckResult:
    return CheckResult(name, category, "fail", **fields)


def warned(name: str, category: Category, **fields) -> CheckResult:
    fields.setdefault("gating", False)
    return CheckResult(name, category, "warn", **fields)


def verdict(condition: bool, name: str, category: Category, **fields) -> CheckResult:
    """`pass` when the condition holds, `fail` when it does not."""
    return CheckResult(name, category, "pass" if condition else "fail", **fields)


def failures(checks: list[CheckResult]) -> list[CheckResult]:
    return [check for check in checks if check.blocking]


def worst(checks: list[CheckResult]) -> Outcome:
    """The phase's own result: any blocking failure fails it, else warn, else pass."""
    if any(check.blocking for check in checks):
        return "fail"
    if any(check.result in ("fail", "warn") for check in checks):
        return "warn"
    return "pass"


@dataclass
class CheckLog:
    """A phase's accumulating list of checks."""

    phase: str
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, check: CheckResult) -> CheckResult:
        self.checks.append(check)
        return check

    def extend(self, checks: list[CheckResult]) -> None:
        self.checks.extend(checks)

    @property
    def blocked(self) -> bool:
        return any(check.blocking for check in self.checks)

    @property
    def result(self) -> Outcome:
        return worst(self.checks)

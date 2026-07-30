"""The `IMPLEMENT` / `FIX_CODE` handler — Stage 5's A2 (Implementation_Plan §8).

Three shapes of one job, distinguished by what the job carries:

* **First iteration**, from `SPEC_SAVED` — `payload["spec_id"]` names a
  hand-written spec that already exists. Pure Python: no LLM call, just
  render -> check -> commit -> open the experiment. This is the path Stage
  5's done-when tests: *"given a hand-written spec, A2 produces code that
  passes P0 and runs through `evaluate.py` unattended."*
* **Plan-driven iteration**, from `REVIEW_ITERATE` — `payload["research_plan_id"]`
  names a below-the-bar verdict (App-Flow §6). `job["experiment_id"]` is the
  *reviewed* experiment; this handler opens the *next* one. A3 (Stage 6)
  does not exist yet, so this path is exercised by inserting a
  `research_plans` row directly and enqueuing `IMPLEMENT` by hand.
* **`FIX_CODE`** — a deterministic failure (a static check, never a research
  finding) routes back with diagnostics attached. `job["experiment_id"]`
  names the experiment already sitting in `code_pending`; this handler
  revises its spec in place rather than opening a new iteration.

Every shape ends at the same place: `render.py` -> `sandbox.py` -> a git
commit -> one `code_versions` row. Only the LLM call (`agents/session.py`)
differs, and it never runs for the first shape at all.

**Bounded retries, then quarantine** (App-Flow §4.3): a failing attempt does
not fail the *job* — the checks ran and produced a verdict, which is a
successful job outcome, exactly like `EVALUATE`'s bar-clear short-circuit.
Persisting that verdict is what decides whether to enqueue another
`FIX_CODE` or quarantine the strategy for human inspection.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ...agents.context import assemble_implement_brief, implement_prompt_version
from ...agents.render import code_path_for, render_module
from ...agents.sandbox import run_sandboxed_checks
from ...agents.session import AgentSession
from ...config import get_settings
from ...db.repositories import (
    CodeVersionRepository,
    EvaluationRepository,
    ExperimentRepository,
    ResearchPlanRepository,
    SpecRepository,
    StrategyRepository,
)
from ...db.repositories.base import Row
from ...eval.checks import CheckResult
from ...operators.spec import StrategySpec
from ...profiles import ProfileLoader
from ...vcs import StrategyRepo
from ..events import Event, emit
from ..states import transition
from .base import HandlerResult

__all__ = ["ImplementOutcome", "persist", "run", "set_session"]

#: Payload keys this handler consumes for itself — everything else is
#: carried through unchanged to the `EVALUATE` job (`asset_class`,
#: `data_snapshot_id`, `campaign`, `cost_multiplier`, `random_seed`, ...).
_HANDLER_ONLY_PAYLOAD_KEYS = frozenset({"spec_id", "research_plan_id", "diagnostics", "fix_attempt"})

_session_override: AgentSession | None = None


def set_session(session: AgentSession | None) -> None:
    """Override the session `run()` calls. Tests only.

    `None` restores the default — a lazily-constructed `AnthropicSession`, so
    importing or calling this handler never requires `ANTHROPIC_API_KEY`
    unless a plan-driven iteration or a `FIX_CODE` retry actually happens.
    """
    global _session_override
    _session_override = session


def _get_session() -> AgentSession:
    if _session_override is not None:
        return _session_override
    from ...agents.session import AnthropicSession

    return AnthropicSession()


@dataclass(frozen=True)
class ImplementOutcome:
    mode: str  # "first_iteration" | "plan_iteration" | "fix_code"
    strategy_id: int
    reviewed_experiment_id: int | None
    research_plan_id: int | None
    spec: StrategySpec
    spec_id: int | None  # set only for "first_iteration", where it already exists
    spec_version: int
    change_summary: str
    prompt_version: str | None
    tokens_spent: int
    branch: str
    code_path: str
    code_hash: str
    commit_hash: str
    git_diff: str
    checks: list[CheckResult]
    checks_passed: bool
    eval_payload: dict[str, Any]


def _eval_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _HANDLER_ONLY_PAYLOAD_KEYS}


def run(conn: sqlite3.Connection, job: Row) -> ImplementOutcome:
    """The heavy, side-effect-free-on-the-database half.

    Reads the database but writes nothing to it — every insert and state
    transition happens in `persist()`, inside the caller's transaction, so a
    crash here (mid-LLM-call, mid-sandbox-run) leaves no partial database
    write to clean up. The one external side effect, the git commit, is
    content-addressed and idempotent (`aqrl.vcs.StrategyRepo`), so a retried
    `run()` after such a crash reproduces the same commit rather than a new
    one.
    """
    strategy_id = job["strategy_id"]
    if strategy_id is None:
        raise ValueError(f"{job['job_type']} job has no strategy_id")

    strategies = StrategyRepository(conn)
    strategy = strategies.get(strategy_id)
    if strategy is None:
        raise ValueError(f"no strategy {strategy_id}")

    payload = job["payload"] or {}
    asset_class = payload.get("asset_class")
    if not asset_class:
        raise ValueError(f"{job['job_type']} job payload missing required 'asset_class'")

    specs = SpecRepository(conn)
    experiments = ExperimentRepository(conn)

    if job["job_type"] == "FIX_CODE":
        reviewed_experiment_id = job["experiment_id"]
        if reviewed_experiment_id is None:
            raise ValueError("FIX_CODE job has no experiment_id")
        target = experiments.get(reviewed_experiment_id)
        if target is None:
            raise ValueError(f"no experiment {reviewed_experiment_id}")

        mode = "fix_code"
        research_plan_id = target.get("research_plan_id")
        prior_spec = specs.load_spec(target["spec_id"])
        prior_versions = CodeVersionRepository(conn).for_experiment(reviewed_experiment_id)
        prior_diff = prior_versions[-1]["diff_from_parent"] if prior_versions else None
        diagnostics = list(payload.get("diagnostics") or [])
        prior_evaluation = None  # a FIX_CODE experiment never reached evaluation
        research_plan = ResearchPlanRepository(conn).get(research_plan_id) if research_plan_id else None
    else:
        research_plan_id = payload.get("research_plan_id")
        if research_plan_id is not None:
            mode = "plan_iteration"
            reviewed_experiment_id = job["experiment_id"]
            if reviewed_experiment_id is None:
                raise ValueError("plan-driven IMPLEMENT job has no experiment_id (the reviewed experiment)")
            reviewed = experiments.get(reviewed_experiment_id)
            if reviewed is None:
                raise ValueError(f"no experiment {reviewed_experiment_id}")
            prior_spec = specs.load_spec(reviewed["spec_id"])
            prior_versions = CodeVersionRepository(conn).for_experiment(reviewed_experiment_id)
            prior_diff = prior_versions[-1]["diff_from_parent"] if prior_versions else None
            prior_evaluation = EvaluationRepository(conn).latest_for_experiment(reviewed_experiment_id)
            research_plan = ResearchPlanRepository(conn).get(research_plan_id)
            diagnostics = []
        else:
            mode = "first_iteration"
            reviewed_experiment_id = None
            spec_id = payload.get("spec_id")
            if spec_id is None:
                raise ValueError("IMPLEMENT job has neither 'spec_id' nor 'research_plan_id' in its payload")
            prior_spec = None
            prior_diff = None
            prior_evaluation = None
            research_plan = None
            diagnostics = []

    if mode == "first_iteration":
        spec_row = specs.get(spec_id)
        if spec_row is None:
            raise ValueError(f"no strategy_specs row {spec_id}")
        final_spec = specs.load_spec(spec_id)
        final_spec_id: int | None = spec_id
        final_spec_version = int(spec_row["version"])
        change_summary = "Initial hand-written specification."
        prompt_version: str | None = None
        tokens_spent = 0
    else:
        brief = assemble_implement_brief(
            strategy=strategy,
            prior_spec=prior_spec,
            research_plan=research_plan,
            prior_diff=prior_diff,
            prior_evaluation=prior_evaluation,
            diagnostics=diagnostics or None,
        )
        prompt_version = implement_prompt_version()
        response = _get_session().propose_spec(brief, prompt_version=prompt_version)
        final_spec = response.spec.to_spec()
        final_spec_id = None  # not yet inserted — persist() does that, inside its transaction
        final_spec_version = specs.next_version(strategy_id)
        change_summary = response.spec.change_summary
        tokens_spent = response.tokens_spent

    resolved = ProfileLoader().resolve(strategy["market"], strategy["timeframe"], asset_class)
    rendered = render_module(final_spec, strategy_uid=strategy["uid"], spec_version=final_spec_version)
    checks = run_sandboxed_checks(rendered.source, spec_hash=final_spec.spec_hash(), resolved=resolved)
    checks_passed = all(check.result == "pass" for check in checks)

    settings = get_settings()
    repo = StrategyRepo(settings.strategy_repo_path)
    branch = f"strategy/{strategy['uid']}"
    code_path = code_path_for(strategy["uid"])
    status_note = "checks passed" if checks_passed else "checks FAILED"
    commit_message = f"{mode}: spec v{final_spec_version} ({status_note})\n\nspec_hash: {final_spec.spec_hash()}"
    commit_hash, git_diff = repo.commit_file(branch, code_path, rendered.source, commit_message)

    return ImplementOutcome(
        mode=mode,
        strategy_id=strategy_id,
        reviewed_experiment_id=reviewed_experiment_id,
        research_plan_id=research_plan_id,
        spec=final_spec,
        spec_id=final_spec_id,
        spec_version=final_spec_version,
        change_summary=change_summary,
        prompt_version=prompt_version,
        tokens_spent=tokens_spent,
        branch=branch,
        code_path=code_path,
        code_hash=rendered.code_hash,
        commit_hash=commit_hash,
        git_diff=git_diff,
        checks=checks,
        checks_passed=checks_passed,
        eval_payload=_eval_payload(payload),
    )


def persist(conn: sqlite3.Connection, job: Row, outcome: ImplementOutcome) -> HandlerResult:
    """The short, atomic half: every database write, in one transaction."""
    settings = get_settings()
    strategies = StrategyRepository(conn)
    experiments = ExperimentRepository(conn)
    specs = SpecRepository(conn)
    code_versions = CodeVersionRepository(conn)

    strategy = strategies.get(outcome.strategy_id)
    if strategy["git_branch"] is None:
        strategies.update(outcome.strategy_id, git_branch=outcome.branch, code_path=outcome.code_path)
        strategy = strategies.get(outcome.strategy_id)

    if outcome.mode == "first_iteration":
        spec_id = outcome.spec_id
    else:
        # Raises DuplicateSpecError on an exact repeat — the transaction
        # rolls back and the worker classifies it as a deterministic job
        # failure, the same path as any other unrecoverable IMPLEMENT error
        # (Implementation_Plan §6's failure classification, unmodified).
        spec_id = specs.insert_spec(
            outcome.spec,
            outcome.strategy_id,
            version=outcome.spec_version,
            prompt_version=outcome.prompt_version,
        )

    if outcome.mode == "fix_code":
        experiment_id = outcome.reviewed_experiment_id
        # Keep the experiment pointed at the spec this attempt actually used.
        experiments.update(experiment_id, spec_id=spec_id)
    else:
        iteration = experiments.next_iteration(outcome.strategy_id)
        experiment_id = experiments.open_pending(
            outcome.strategy_id,
            iteration,
            spec_id,
            parent_experiment_id=outcome.reviewed_experiment_id,
            research_plan_id=outcome.research_plan_id,
        )
        transition(conn, "experiments", experiment_id, "code_pending", actor="agent:A2")
        strategies.update(outcome.strategy_id, current_experiment_id=experiment_id)
        if strategy["status"] in ("spec_ready", "iterating"):
            transition(conn, "strategies", outcome.strategy_id, "coding", actor="agent:A2")
            strategy = strategies.get(outcome.strategy_id)

    code_version_id = code_versions.insert(
        experiment_id=experiment_id,
        code_path=outcome.code_path,
        code_hash=outcome.code_hash,
        git_commit=outcome.commit_hash,
        diff_from_parent=outcome.git_diff,
        change_summary=outcome.change_summary,
        implements_plan_id=outcome.research_plan_id,
        compile_ok=outcome.checks_passed,
        static_check_results=[check.row() for check in outcome.checks],
        prompt_version=outcome.prompt_version,
    )
    experiments.update(experiment_id, code_version_id=code_version_id)

    if outcome.checks_passed:
        transition(conn, "experiments", experiment_id, "code_ready", actor="agent:A2")
        transition(conn, "experiments", experiment_id, "evaluating", actor="agent:A2")
        if strategy["status"] == "coding":
            transition(conn, "strategies", outcome.strategy_id, "evaluating", actor="agent:A2")
        emit(
            conn,
            Event.CODE_CHECKS_PASSED,
            strategy_id=outcome.strategy_id,
            experiment_id=experiment_id,
            payload=outcome.eval_payload,
        )
        return HandlerResult(tokens_spent=outcome.tokens_spent)

    # Checks failed: a bug, never a research finding (App-Flow §4.3). Bounded
    # retries via FIX_CODE, then quarantine for human inspection.
    attempts = code_versions.count(experiment_id=experiment_id)
    diagnostics = [check.detail for check in outcome.checks if check.result == "fail" and check.detail]

    if attempts >= settings.max_fix_attempts:
        experiments.complete(experiment_id, status="failed", outcome="error", failure_reason="code_error")
        if not strategy["quarantined"]:
            reason = f"{attempts} consecutive static-check failures on experiment {experiment_id}"
            transition(
                conn,
                "strategies",
                outcome.strategy_id,
                "quarantined",
                actor="agent:A2",
                reasoning=reason,
                quarantined=True,
                quarantine_reason=reason,
            )
        return HandlerResult(tokens_spent=outcome.tokens_spent)

    emit(
        conn,
        Event.CODE_CHECKS_FAILED,
        strategy_id=outcome.strategy_id,
        experiment_id=experiment_id,
        payload={**outcome.eval_payload, "diagnostics": diagnostics, "fix_attempt": attempts},
        dedupe_key=f"code_checks_failed:{experiment_id}:{attempts}",
    )
    return HandlerResult(tokens_spent=outcome.tokens_spent)

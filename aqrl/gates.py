"""Human Gates — Stage 9 (Implementation_Plan §12), the safety boundary.

**A database write is the trigger everywhere else in this system** (TRD
§4.1); here it is a *human* write. `handlers/promote.py`'s A4 never gets to
say yes alone — `requires_human_approval` is hardcoded 1 on every promotion
row it writes, and nothing before this module ever read `human_decision`
back. `approve` is the only code path in the whole codebase that merges a
strategy branch, opens the vault, or inserts into `deployments` (TRD §18:
*"no code path may bypass these gates"*).

Lives at package root, next to `vcs.py`, not under `orchestration/handlers/`
— this is not a job. `events.py`'s `EVENT_JOB_TYPE` table already marks
`Event.HUMAN_APPROVED_GATE` as `job_type=None`, annotated *"creates a
deployment + merges the branch directly (TRD §5.3)"*; `cli.py`'s `aqrl
review` subcommands are a thin argparse shell over the functions below.

**Vault: gate only, not the full App-Flow §15 scoring flow.** `approve` logs
the open and decrements the family's lifetime budget; it does not load the
vault snapshot or re-score the strategy against it — that needs a vault
snapshot that does not exist yet. `vault_access_log.result_score`/`outcome`
stay `NULL` here, a known limit rather than a shortcut papered over.

**Rejection reaches A5's knowledge base by direct write, not by re-running
A5.** `ARCHIVE` already fired on `PROMOTION_DECIDED`, before the human ever
saw this queue, and `archive.py`'s `lab_notebooks` idempotency guard makes a
second `ARCHIVE` for the same strategy a no-op (TRD §12.1: "A5 runs once,
ever"). `reject` writes the `knowledge_entries` row itself — human-authored,
no LLM call — in the same `evidence.failure_reasons` shape
`db.repositories.knowledge.repeat_failure_rate` already reads, so a human
rejection counts toward that metric exactly like an A5-recorded one.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .agents.context import promotion_evidence_sections
from .config import get_settings
from .db import transaction
from .db.repositories import (
    DeploymentRepository,
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    KnowledgeEntryRepository,
    LifecycleEventRepository,
    PromotionRepository,
    ResearchGoalRepository,
    ResearchQuestionRepository,
    SnapshotRepository,
    SpecRepository,
    StrategyRepository,
    VaultAccessRepository,
)
from .db.repositories.base import Row, utcnow_iso
from .eval.regimes import REGIMES
from .orchestration.events import Event, emit
from .orchestration.states import transition
from .profiles import ProfileLoader

__all__ = ["FAILURE_REASONS", "approve", "defer", "evidence", "pending", "reject"]

#: `promotions.stage_to` -> (deploy branch, deployment mode, strategies.status
#: on approval). Only Gate 1 ("research" -> "human_review") has a real
#: producer today — `handlers/promote.py` hardcodes that value on every
#: promotion row it writes. Gate 2 ("live_small") is wired for the day
#: Stage 11 starts producing those rows (TRD §5.3's paper->live edge), not
#: exercised by anything yet.
_STAGE_TO_TARGET: dict[str, tuple[str, str, str]] = {
    "human_review": ("deploy/paper", "paper", "paper_trading"),
    "live_small": ("deploy/live", "live", "live_small"),
}

#: Mirrors `experiments.failure_reason`'s CHECK constraint (0002_core_research.sql)
#: — the system's one structured failure vocabulary, reused rather than
#: inventing a second enum for a human's rejection reason. Same precedent
#: `archive.py`'s `_BUG_FAILURE_REASONS` already set for a subset of this list.
FAILURE_REASONS = frozenset(
    {
        "no_signal", "negative_expectancy", "costs_exceed_edge", "overfit_in_sample",
        "walk_forward_unstable", "regime_dependent", "pbo_too_high",
        "deflated_sharpe_insufficient", "insufficient_trades", "monte_carlo_ruin_risk",
        "parameter_sensitive", "capacity_constrained", "plateaued_below_bar",
        "code_error", "look_ahead_detected", "data_leakage_detected",
    }
)


def pending(conn: sqlite3.Connection) -> list[Row]:
    """Every promotion still awaiting a human (`aqrl review list`)."""
    return PromotionRepository(conn).pending_human_decision()


def evidence(conn: sqlite3.Connection, promotion: Row) -> str:
    """Render exactly the evidence package A4 judged this promotion on
    (`aqrl review show`) — the same `promotion_evidence_sections` call
    `assemble_promote_brief` makes, so there is no second renderer to drift
    out of sync with what A4 actually saw."""
    strategy = StrategyRepository(conn).get(promotion["strategy_id"])
    if strategy is None:
        raise ValueError(f"no strategy {promotion['strategy_id']}")

    experiments = ExperimentRepository(conn)
    winning_experiment = experiments.get(promotion["best_experiment_id"])
    if winning_experiment is None:
        raise ValueError(f"no experiment {promotion['best_experiment_id']}")

    spec = SpecRepository(conn).load_spec(winning_experiment["spec_id"])
    experiment_history = experiments.find(strategy_id=strategy["id"], order_by="iteration")
    winning_evaluation = EvaluationRepository(conn).latest_for_experiment(winning_experiment["id"])
    if winning_evaluation is None:
        raise ValueError(f"no evaluation for experiment {winning_experiment['id']}")
    diagnostic_checks = EvaluationTestRepository(conn).for_evaluation(winning_evaluation["id"])

    return promotion_evidence_sections(
        strategy=strategy,
        spec=spec,
        experiment_history=experiment_history,
        winning_evaluation=winning_evaluation,
        diagnostic_checks=diagnostic_checks,
        iteration_count=promotion.get("iterations_considered") or strategy["iteration_count"],
    )


def approve(
    conn: sqlite3.Connection,
    promotion_id: int,
    *,
    by: str,
    note: str,
    vault_segment: str | None = None,
    capital_minor_units: int | None = None,
) -> dict[str, Any]:
    """Human Gate — approve. Opens the vault, merges the branch, creates the
    deployment. Every check below runs, and every write below happens,
    before `human_decision` ever reads anything but `'pending'` for this row.

    The git merge runs OUTSIDE the transaction — same `run`/`persist` split
    `implement.py` uses for `commit_file`, because a merge must never hold
    SQLite's write lock. `StrategyRepo.merge` is idempotent by git's own
    semantics (an already-merged branch just returns HEAD), so a crash
    between the merge and the transaction below is safe to recover by
    re-running `approve` — it re-derives the same merge commit.

    `vcs.StrategyRepo` is imported right before its one use, not at module
    level: it pulls in `vcs.py`'s `fcntl` dependency, unavailable on a
    platform without it, and every guard above this point (bad note, unknown
    promotion, already-decided, exhausted vault budget) must still raise its
    own clear error rather than fail on an unrelated import.
    """
    if not note or not note.strip():
        raise ValueError("an approval requires a typed note (Backend-Schema §7 — friction on purpose)")

    promotions = PromotionRepository(conn)
    promotion = promotions.get(promotion_id)
    if promotion is None:
        raise ValueError(f"no promotion {promotion_id}")
    if promotion["human_decision"] != "pending":
        raise ValueError(f"promotion {promotion_id} is already {promotion['human_decision']}")

    strategy = StrategyRepository(conn).get(promotion["strategy_id"])
    if strategy is None:
        raise ValueError(f"no strategy {promotion['strategy_id']}")

    try:
        deploy_branch, mode, target_status = _STAGE_TO_TARGET[promotion["stage_to"]]
    except KeyError:
        raise ValueError(
            f"promotion {promotion_id} has unrecognised stage_to {promotion['stage_to']!r}"
        ) from None

    # The vault: opened only at promotion, once per strategy family, logged
    # and budget-decremented before anything else happens (TRD §15.2). A
    # family that has already spent its budget is BLOCKED, full stop.
    family = strategy["family"]
    vault = VaultAccessRepository(conn)
    settings = get_settings()
    budget_before = settings.vault_budget_per_family - vault.opens_for_family(family)
    if budget_before <= 0:
        raise PermissionError(
            f"family {family!r} has exhausted its vault budget "
            f"({settings.vault_budget_per_family} lifetime open(s)); it cannot be promoted again "
            "until genuinely new data exists (TRD §15.2)"
        )

    from .vcs import StrategyRepo  # deferred: pulls in vcs.py's fcntl dependency

    repo = StrategyRepo(settings.strategy_repo_path)
    merge_message = f"promote {strategy['name']} ({promotion['uid']})\n\npromotion_uid: {promotion['uid']}"
    merge_commit = repo.merge(deploy_branch, strategy["git_branch"], merge_message)

    experiments = ExperimentRepository(conn)
    winning_experiment = experiments.get(promotion["best_experiment_id"])
    winning_evaluation = (
        EvaluationRepository(conn).latest_for_experiment(winning_experiment["id"])
        if winning_experiment is not None
        else None
    )
    snapshot_id = winning_experiment.get("data_snapshot_id") if winning_experiment else None
    if snapshot_id is None:
        raise ValueError(f"experiment {promotion['best_experiment_id']} has no data_snapshot_id")
    snapshot = SnapshotRepository(conn).get(snapshot_id)
    if snapshot is None:
        raise ValueError(f"no snapshot {snapshot_id}")
    resolved = ProfileLoader().resolve(strategy["market"], strategy["timeframe"], snapshot["asset_class"])

    with transaction(conn, immediate=True):
        budget_after = budget_before - 1
        VaultAccessRepository(conn).insert(
            strategy_id=strategy["id"],
            family=family,
            vault_segment=vault_segment,
            opened_at=utcnow_iso(),
            opened_by="promotion_gate",
            reason=note,
            promotion_id=promotion_id,
            budget_before=budget_before,
            budget_after=budget_after,
        )

        promotions.update(
            promotion_id,
            human_decision="approved",
            human_decided_by=by,
            human_decided_at=utcnow_iso(),
            human_notes=note,
            merge_commit=merge_commit,
        )

        deployment_id = DeploymentRepository(conn).insert(
            strategy_id=strategy["id"],
            experiment_id=winning_experiment["id"] if winning_experiment else None,
            mode=mode,
            status="active",
            deploy_branch=deploy_branch,
            allocation_pct=promotion.get("recommended_allocation_pct"),
            capital_minor_units=capital_minor_units,
            currency=resolved.market.reference.currency,
            started_at=utcnow_iso(),
            # PRD §9.3's promotion gate: trade count AND deviation AND regime
            # coverage AND execution AND health — the four regimes PRD §9.3
            # names, `crisis` excluded (too rare to gate an otherwise-healthy
            # strategy on).
            trades_required=resolved.timeframe.min_trades,
            trades_completed=0,
            regimes_required=[r for r in REGIMES if r != "crisis"],
            regimes_observed=[],
            expected_sharpe=winning_evaluation.get("sharpe") if winning_evaluation else None,
            expected_max_dd=winning_evaluation.get("max_drawdown") if winning_evaluation else None,
            expected_win_rate=winning_evaluation.get("win_rate") if winning_evaluation else None,
            expected_avg_trade=winning_evaluation.get("avg_trade_return") if winning_evaluation else None,
        )

        transition(conn, "strategies", strategy["id"], target_status, actor=f"human:{by}", reasoning=note)

        LifecycleEventRepository(conn).insert(
            deployment_id=deployment_id,
            strategy_id=strategy["id"],
            event_type="deployed",
            from_state=strategy["status"],
            to_state=target_status,
            triggered_by="human",
            reason=note,
            evidence={"promotion_id": promotion_id, "merge_commit": merge_commit},
        )

        emit(
            conn,
            Event.HUMAN_APPROVED_GATE,
            strategy_id=strategy["id"],
            payload={
                "promotion_id": promotion_id,
                "deployment_id": deployment_id,
                "mode": mode,
                "merge_commit": merge_commit,
            },
            actor=f"human:{by}",
            reasoning=note,
        )

    return {
        "promotion_id": promotion_id,
        "deployment_id": deployment_id,
        "merge_commit": merge_commit,
        "deploy_branch": deploy_branch,
        "mode": mode,
        "strategy_status": target_status,
    }


def reject(
    conn: sqlite3.Connection,
    promotion_id: int,
    *,
    by: str,
    note: str,
    reason: str,
    next_questions: list[str],
) -> dict[str, Any]:
    """Human Gate — reject. No git, no vault. Writes the lesson straight
    into `knowledge_entries` and pushes each `next_question` onto the
    curiosity queue — see the module docstring for why this does not
    re-invoke A5."""
    if not note or not note.strip():
        raise ValueError("a rejection requires a typed note")
    if reason not in FAILURE_REASONS:
        raise ValueError(f"unrecognised rejection reason {reason!r} (expected one of {sorted(FAILURE_REASONS)})")
    if not next_questions:
        raise ValueError("a rejection requires at least one --next-question (future_ideas is mandatory, TRD §12.1)")

    promotions = PromotionRepository(conn)
    promotion = promotions.get(promotion_id)
    if promotion is None:
        raise ValueError(f"no promotion {promotion_id}")
    if promotion["human_decision"] != "pending":
        raise ValueError(f"promotion {promotion_id} is already {promotion['human_decision']}")

    strategy = StrategyRepository(conn).get(promotion["strategy_id"])
    if strategy is None:
        raise ValueError(f"no strategy {promotion['strategy_id']}")

    best_experiment_id = promotion.get("best_experiment_id")

    with transaction(conn, immediate=True):
        promotions.update(
            promotion_id,
            human_decision="rejected",
            human_decided_by=by,
            human_decided_at=utcnow_iso(),
            human_notes=note,
        )

        transition(
            conn, "strategies", strategy["id"], "rejected",
            actor=f"human:{by}", reasoning=note, evidence={"failure_reason": reason},
        )

        entry_id = KnowledgeEntryRepository(conn).record(
            entry_type="lesson",
            scope="family",
            strategy_id=strategy["id"],
            experiment_id=best_experiment_id,
            title=f"Human rejection: {strategy['name']}",
            statement=note,
            # `failure_reasons` is the structured field `repeat_failure_rate`
            # reads — this is what makes a human rejection count exactly
            # like an A5-recorded lesson.
            evidence={
                "experiment_ids": [best_experiment_id] if best_experiment_id else [],
                "failure_reasons": [reason],
            },
            evidence_count=1,
            applicable_markets=[strategy["market"]],
            applicable_timeframes=[strategy["timeframe"]],
            future_ideas=next_questions,
        )

        question_ids = [
            ResearchQuestionRepository(conn).push(
                question, origin_type="human", origin_experiment_id=best_experiment_id
            )
            for question in next_questions
        ]

    return {
        "promotion_id": promotion_id,
        "knowledge_entry_id": entry_id,
        "research_question_ids": question_ids,
        "strategy_status": "rejected",
    }


def defer(conn: sqlite3.Connection, promotion_id: int, *, by: str, note: str) -> dict[str, Any]:
    """Human Gate — defer (Stage 12, UI-UX-Brief §3.3's third button;
    App-Flow §9's DEFER: *"request more research (creates a new goal)"*).
    No git, no vault — the same "no execution authority beyond a database
    write" shape `reject` already has.

    Mirrors A4's own defer path (`orchestration/handlers/promote.py:178`) at
    the human's own authority: `strategies.status` is deliberately left
    untouched — the idea re-enters research via a fresh `research_goals`
    row, not by resuming this strategy's own loop.

    `promotions.human_decision`'s CHECK has no 'deferred' value (only
    approved/rejected/pending — Backend-Schema §7), so the row stays
    'pending' there; `deferred_at` (`0010_promotion_defer.sql`) is what
    actually removes it from `pending_human_decision()`'s queue.
    """
    if not note or not note.strip():
        raise ValueError("a deferral requires a typed note")

    promotions = PromotionRepository(conn)
    promotion = promotions.get(promotion_id)
    if promotion is None:
        raise ValueError(f"no promotion {promotion_id}")
    if promotion["human_decision"] != "pending":
        raise ValueError(f"promotion {promotion_id} is already {promotion['human_decision']}")
    if promotion.get("deferred_at"):
        raise ValueError(f"promotion {promotion_id} is already deferred")

    strategy = StrategyRepository(conn).get(promotion["strategy_id"])
    if strategy is None:
        raise ValueError(f"no strategy {promotion['strategy_id']}")

    with transaction(conn, immediate=True):
        promotions.update(
            promotion_id,
            deferred_at=utcnow_iso(),
            human_decided_by=by,
            human_decided_at=utcnow_iso(),
            human_notes=note,
        )
        goal_id = ResearchGoalRepository(conn).insert(
            title=f"Deferred by human: {strategy['name']}",
            description=note,
            market=strategy.get("market"),
            timeframe=strategy.get("timeframe"),
            allocation_bucket="incremental",
            status="active",
            created_by="human",
        )

    return {
        "promotion_id": promotion_id,
        "research_goal_id": goal_id,
        "strategy_status": strategy["status"],
    }

"""Repositories for the research loop: strategies, experiments, evaluations.

These carry the queries Backend-Schema §15 says must be fast, most importantly
`family_trial_count` — the deflated Sharpe is only honest if the trial count
behind it is exact, and *"how many attempts in this family?"* is precisely the
question git cannot answer (TRD §5.1).
"""
from __future__ import annotations

from typing import Any

from .base import Repository, Row, utcnow_iso

# nanoAQRL's `results.tsv` records exactly three verdicts (TRD §2.2). They are a
# *verdict*, not a lifecycle state, so they map onto the canonical
# `experiments.status` / `outcome` pair rather than replacing it.
VERDICT_TO_STATUS: dict[str, tuple[str, str]] = {
    "keep": ("evaluated", "passed"),
    "discard": ("evaluated", "failed"),
    "crash": ("error", "error"),
}


class StrategyRepository(Repository):
    table = "strategies"
    updated_column = "updated_at"

    def get_or_create(
        self,
        name: str,
        family: str,
        market: str,
        timeframe: str,
        **fields: Any,
    ) -> int:
        existing = self.conn.execute(
            "SELECT id FROM strategies WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            return int(existing["id"])
        return self.insert(name=name, family=family, market=market, timeframe=timeframe, **fields)

    def family_trial_count(self, family: str) -> int:
        """Prior iterations across every strategy in this family.

        Backend-Schema §4: *"the same idea tried in 3 markets is 3 trials in one
        family, not 3 independent results."* Returns the RAW count — the caller
        adds the current attempt and applies the best-of-three x3 factor exactly
        once, so the multiplier is never applied twice (TRD §8.6).
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(iteration_count), 0) AS total FROM strategies WHERE family = ?",
            (family,),
        ).fetchone()
        return int(row["total"])

    def family_trial_counts_for(self, market: str, timeframe: str) -> dict[str, int]:
        """`family_trial_count`, for every family already active in this
        market/timeframe — Stage 7's Research Brief needs the whole
        landscape (App-Flow §3.2's "trials already spent in this family"),
        not one family at a time, since A1 hasn't named a family yet."""
        rows = self.conn.execute(
            "SELECT family, SUM(iteration_count) AS total FROM strategies "
            "WHERE market = ? AND timeframe = ? GROUP BY family",
            (market, timeframe),
        ).fetchall()
        return {row["family"]: int(row["total"]) for row in rows}

    def record_bar_clear(self, strategy_id: int, experiment_id: int, score: float) -> None:
        """Clearing the bar is an immediate, unconditional stop (PRD §9.2)."""
        self.update(
            strategy_id,
            best_experiment_id=experiment_id,
            best_score=score,
            status="pending_promotion",
        )

    def record_bar_failure(self, strategy_id: int) -> int:
        """Increment the plateau counter and return its new value.

        Only ever increments. Clearing the bar stops the loop immediately, so
        there is no "improvement" case to reset against (Backend-Schema §4).
        """
        self.conn.execute(
            "UPDATE strategies SET plateau_counter = plateau_counter + 1, updated_at = ? WHERE id = ?",
            (utcnow_iso(), strategy_id),
        )
        row = self.conn.execute(
            "SELECT plateau_counter FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        return int(row["plateau_counter"])


class ExperimentRepository(Repository):
    table = "experiments"
    json_columns = frozenset({"metrics_json"})

    def next_iteration(self, strategy_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(iteration), 0) AS max_iter FROM experiments WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        return int(row["max_iter"]) + 1

    def start(self, strategy_id: int, iteration: int, **provenance: Any) -> int:
        """Open an experiment row before doing the work.

        It starts in `evaluating`: if the process dies mid-run the row is
        visibly unfinished rather than absent, which is what makes a crash
        distinguishable from "never happened". Used by the ad hoc
        `aqrl evaluate run` CLI path, which has code and a snapshot in hand
        already — Stage 5's `IMPLEMENT` handler uses `open_pending` instead,
        since it starts before code exists (Backend-Schema §14.2's
        `created` -> `code_pending` -> `code_ready` -> `evaluating` chain).
        """
        return self.insert(
            strategy_id=strategy_id, iteration=iteration, status="evaluating", **provenance
        )

    def open_pending(
        self,
        strategy_id: int,
        iteration: int,
        spec_id: int,
        *,
        parent_experiment_id: int | None = None,
        research_plan_id: int | None = None,
    ) -> int:
        """Open an experiment before any code exists for it.

        Starts in `created` — the head of `EXPERIMENT_TRANSITIONS`
        (`aqrl/orchestration/states.py`) — because Stage 5's `IMPLEMENT`
        handler must record the attempt *before* rendering, checking, or
        committing anything, so a worker crash mid-codegen leaves a visibly
        unfinished row rather than no row at all (App-Flow §4, same
        crash-distinguishability argument as `start()` above).
        """
        return self.insert(
            strategy_id=strategy_id,
            iteration=iteration,
            spec_id=spec_id,
            parent_experiment_id=parent_experiment_id,
            research_plan_id=research_plan_id,
            status="created",
        )

    def complete(
        self,
        experiment_id: int,
        status: str,
        outcome: str,
        phase_reached: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        self.update(
            experiment_id,
            status=status,
            outcome=outcome,
            phase_reached=phase_reached,
            failure_reason=failure_reason,
            completed_at=utcnow_iso(),
        )
        self.conn.execute(
            """UPDATE strategies
                  SET iteration_count = iteration_count + 1, updated_at = ?
                WHERE id = (SELECT strategy_id FROM experiments WHERE id = ?)""",
            (utcnow_iso(), experiment_id),
        )

    def complete_from_verdict(
        self,
        experiment_id: int,
        verdict: str,
        phase_reached: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Close an experiment from a nanoAQRL `keep`/`discard`/`crash` verdict."""
        try:
            status, outcome = VERDICT_TO_STATUS[verdict]
        except KeyError:
            raise ValueError(f"unknown verdict {verdict!r}; expected one of {sorted(VERDICT_TO_STATUS)}") from None
        self.complete(experiment_id, status, outcome, phase_reached, failure_reason)

    def comparable_to(
        self,
        eval_engine_version: str,
        market_profile_hash: str,
        timeframe_profile_hash: str,
        wf_config_hash: str,
    ) -> list[Row]:
        """Backend-Schema §15 Q2 — *"which stored results are still comparable?"*

        Answered from `idx_experiments_provenance`, never a scan. Without this,
        the knowledge base silently mixes results scored under different rules.
        """
        rows = self.conn.execute(
            """SELECT * FROM experiments
                WHERE eval_engine_version = ? AND market_profile_hash = ?
                  AND timeframe_profile_hash = ? AND wf_config_hash = ?""",
            (eval_engine_version, market_profile_hash, timeframe_profile_hash, wf_config_hash),
        ).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[misc]

    def mark_incomparable(self, **provenance: Any) -> int:
        """Flag stored results as no longer comparable. They are never deleted."""
        clauses = " AND ".join(f"{column} = ?" for column in provenance)
        cursor = self.conn.execute(
            f"UPDATE experiments SET comparable = 0 WHERE {clauses}", list(provenance.values())
        )
        return cursor.rowcount


class EvaluationRepository(Repository):
    table = "evaluations"
    json_columns = frozenset(
        {"wf_train_years_evaluated", "tuned_params_per_fold", "fold_metrics", "metrics_json"}
    )

    def for_experiment(self, experiment_id: int) -> list[Row]:
        return self.find(experiment_id=experiment_id, order_by="id")

    def latest_for_experiment(self, experiment_id: int, phase: str | None = None) -> Row | None:
        sql = "SELECT * FROM evaluations WHERE experiment_id = ?"
        params: list[Any] = [experiment_id]
        if phase:
            sql += " AND phase = ?"
            params.append(phase)
        sql += " ORDER BY id DESC LIMIT 1"
        return self._decode(self.conn.execute(sql, params).fetchone())


class EvaluationTestRepository(Repository):
    """One row per test within a phase — `evaluation_tests` has no `uid` or
    `created_at`, so both Repository conventions are switched off."""

    table = "evaluation_tests"
    has_uid = False
    created_column = None

    def for_evaluation(self, evaluation_id: int) -> list[Row]:
        return self.find(evaluation_id=evaluation_id)


class RegimePerformanceRepository(Repository):
    """Per-regime performance rows — same no-`uid`/no-timestamp shape."""

    table = "regime_performance"
    has_uid = False
    created_column = None

    def for_evaluation(self, evaluation_id: int) -> list[Row]:
        return self.find(evaluation_id=evaluation_id)


class CodeVersionRepository(Repository):
    """Every implementation A2 produces (Backend-Schema §5).

    Recorded even for a rejected attempt: `compile_ok` and
    `static_check_results` exist precisely so a `FIX_CODE` loop's history is
    legible afterward — *why* did it take three tries, not just that it did.
    """

    table = "code_versions"
    json_columns = frozenset({"static_check_results"})

    def for_experiment(self, experiment_id: int) -> list[Row]:
        return self.find(experiment_id=experiment_id, order_by="id")


class ResearchPlanRepository(Repository):
    """A3's output (Backend-Schema §6). **Never contains code** — it is a
    research instruction that Stage 5's context assembler reads, not writes;
    A3 itself is Stage 6 and does not exist yet. Stage 5 needs read access
    now because the `IMPLEMENT` handler's iteration path (a spec revised in
    response to a plan) is exercised by inserting a `research_plans` row by
    hand until Stage 6 ships a real producer.
    """

    table = "research_plans"
    json_columns = frozenset({"evidence_cited", "proposed_changes"})

    def for_experiment(self, experiment_id: int) -> list[Row]:
        return self.find(experiment_id=experiment_id, order_by="id")


class NullWorldRunRepository(Repository):
    table = "null_world_runs"

    def record(
        self,
        null_model: str,
        replications: int,
        discoveries_reported: int,
        max_score_observed: float,
        **fields: Any,
    ) -> int:
        """Record a calibration run, deriving FDR and the verdict.

        Milestone 0: *"a discovery from an uncalibrated pipeline is not a
        discovery."* The rate is derived here rather than passed in so it can
        never disagree with the counts beside it.
        """
        rate = discoveries_reported / replications if replications else 0.0
        fields.setdefault("verdict", "pipeline_trusted" if discoveries_reported == 0 else "pipeline_suspect")
        return self.insert(
            null_model=null_model,
            replications=replications,
            discoveries_reported=discoveries_reported,
            false_discovery_rate=rate,
            max_score_observed=max_score_observed,
            **fields,
        )


class ResearchGoalRepository(Repository):
    """Top-level research direction (Backend-Schema §3). Drives A1's
    hypothesis budget (Stage 7, Implementation_Plan §10) — build-on-need's
    first felt caller for this table; nothing before Stage 7 wrote to it."""

    table = "research_goals"
    updated_column = "updated_at"

    def active_with_budget(self) -> list[Row]:
        """Every goal `fire_due_hypothesis_batch` (scheduler.py) may enqueue
        against — active, and either unbudgeted (NULL = unlimited, the same
        "no row/no cap" convention `budgets.py` uses) or with room left."""
        rows = self.conn.execute(
            "SELECT * FROM research_goals WHERE status = 'active' "
            "AND (hypothesis_budget IS NULL OR hypotheses_used < hypothesis_budget)"
        ).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[misc]

    def increment_hypotheses_used(self, goal_id: int) -> None:
        self.conn.execute(
            "UPDATE research_goals SET hypotheses_used = hypotheses_used + 1, updated_at = ? WHERE id = ?",
            (utcnow_iso(), goal_id),
        )


class ResearchQuestionRepository(Repository):
    """The curiosity queue (Backend-Schema §10). Read-only from Stage 7's
    side — A5 (Stage 8) is the writer; this exists so the Research Brief has
    something to call today, same relationship Stage 6's
    `KnowledgeEntryRepository` has to Stage 8's writer."""

    table = "research_questions"
    json_columns = frozenset({"search_terms", "answer_knowledge_ids", "produced_spec_ids"})

    def open_questions(self, limit: int = 20) -> list[Row]:
        """Priority-ordered open questions. Not scoped to a research goal —
        `research_questions` carries no `goal_id` column (App-Flow §3.2's
        "open research_questions for this goal" bullet presupposes a
        linkage the schema doesn't have); see `context._open_questions_section`
        for the same note where it matters to the brief's reader."""
        return self.find(status="open", order_by="priority DESC, id", limit=limit)

    def push(
        self,
        question: str,
        *,
        motivation: str | None = None,
        origin_type: str,
        origin_experiment_id: int | None = None,
        origin_knowledge_id: int | None = None,
        priority: int = 0,
    ) -> int:
        """A5's write path into the curiosity queue (TRD §12.4,
        Backend-Schema §10). Always opens `status='open'` — the queue's own
        default; whether a collector ever answers it is Stage 10's concern."""
        return self.insert(
            question=question,
            motivation=motivation,
            origin_type=origin_type,
            origin_experiment_id=origin_experiment_id,
            origin_knowledge_id=origin_knowledge_id,
            priority=priority,
            status="open",
        )

    def mark_searching(self, question_id: int, *, search_terms: list[str]) -> None:
        """Stage 10's `COLLECT_PAPERS` consulted this question — App-Flow
        §12's "targeted mode." Does not require the question stay `open`
        first: a question already `searching` from a prior collector run
        may be consulted again (another cadence, another source), and each
        pass's terms overwrite the last rather than accumulate — the terms
        that mattered are whatever produced the eventual answer, visible on
        `record_answer`'s row, not the full history of guesses."""
        self.update(question_id, status="searching", search_terms=search_terms)

    def record_answer(self, question_id: int, knowledge_ids: list[int]) -> None:
        """`EXTRACT_KNOWLEDGE` found something answering this question —
        closes the curiosity loop's first half (App-Flow §12: collector
        finds -> Librarian extracts). `answer_knowledge_ids` accumulates
        rather than overwrites, since more than one document may eventually
        answer the same question."""
        question = self.get(question_id)
        if question is None:
            raise ValueError(f"no research_questions row {question_id}")
        merged = sorted(set(question.get("answer_knowledge_ids") or []) | set(knowledge_ids))
        self.update(question_id, status="answered", answer_knowledge_ids=merged, resolved_at=utcnow_iso())

    def answered_by(self, external_knowledge_ids: list[int]) -> list[Row]:
        """Every `answered` question whose `answer_knowledge_ids` overlaps
        `external_knowledge_ids` — the read-only half of
        `record_produced_spec`, exposed separately so a caller can look
        this up *before* a `spec_id` exists yet (e.g. to set
        `strategy_specs.source_question_id` at insert time)."""
        if not external_knowledge_ids:
            return []
        cited = set(external_knowledge_ids)
        return [
            question
            for question in self.find(status="answered")
            if set(question.get("answer_knowledge_ids") or []) & cited
        ]

    def record_produced_spec(self, external_knowledge_ids: list[int], spec_id: int) -> list[int]:
        """The loop-closure write (`produced_spec_ids`, TRD §12.4): *"did
        asking this ever pay off?"* Any answered question whose
        `answer_knowledge_ids` overlaps the ideas A1 actually cited gets
        `spec_id` appended. Returns the ids of every question updated —
        usually zero or one, but a spec drawing on ideas from two different
        questions' answers updates both.
        """
        touched: list[int] = []
        for question in self.answered_by(external_knowledge_ids):
            produced = sorted(set(question.get("produced_spec_ids") or []) | {spec_id})
            self.update(question["id"], produced_spec_ids=produced)
            touched.append(question["id"])
        return touched

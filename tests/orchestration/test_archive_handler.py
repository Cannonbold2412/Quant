"""The `ARCHIVE` and `MINE_PATTERNS` handlers (Stage 8 — Implementation_Plan
§11): A5's per-strategy write-once record, and cross-experiment mining.
"""
from __future__ import annotations

import pytest

from aqrl.agents.session import (
    KnowledgeEdgeDraft,
    KnowledgeEntryDraft,
    LabNotebookDraft,
    ProposedKnowledge,
    ResearchQuestionDraft,
    StubKnowledgeSession,
)
from aqrl.db import transaction
from aqrl.db.repositories import (
    ExperimentRepository,
    JobRepository,
    KnowledgeEdgeRepository,
    KnowledgeEntryRepository,
    LabNotebookRepository,
    ResearchQuestionRepository,
    StrategyRepository,
)
from aqrl.orchestration.handlers import archive as handler

from .conftest import MARKET, TIMEFRAME


@pytest.fixture(autouse=True)
def _reset_session():
    handler.set_session(None)
    yield
    handler.set_session(None)


@pytest.fixture
def experiment_id(conn, strategy_id: int, spec_id: int, snapshot_id: int) -> int:
    experiments = ExperimentRepository(conn)
    iteration = experiments.next_iteration(strategy_id)
    exp_id = experiments.start(
        strategy_id, iteration, spec_id=spec_id, data_snapshot_id=snapshot_id, code_commit="deadbeef"
    )
    experiments.complete(exp_id, status="reviewed", outcome="plateaued", failure_reason="plateaued_below_bar")
    return exp_id


def _archive_job(strategy_id: int, experiment_id: int) -> dict:
    return {"job_type": "ARCHIVE", "strategy_id": strategy_id, "experiment_id": experiment_id, "payload": {}}


def _knowledge(**overrides) -> ProposedKnowledge:
    fields = dict(
        notebook=LabNotebookDraft(
            hypothesis="h", result="plateaued", reason="no signal past filter tightening", evidence="e",
            confidence=0.3, next_questions=["try a different filter family"],
        ),
        entries=[
            KnowledgeEntryDraft(
                entry_type="lesson", scope="family", title="t", statement="s",
                evidence_experiment_ids=[1], documents_failure_reasons=["plateaued_below_bar"],
                applicable_markets=[MARKET], applicable_timeframes=[TIMEFRAME], future_ideas=["idea"],
            )
        ],
        edges=[
            KnowledgeEdgeDraft(subject="momentum", predicate="fails_in", object="sideways", evidence_experiment_ids=[1])
        ],
        questions=[ResearchQuestionDraft(question="does volatility clustering help?", priority=1)],
    )
    fields.update(overrides)
    return ProposedKnowledge(**fields)


# -- run_archive(): validation --------------------------------------------------


def test_run_archive_requires_strategy_and_experiment_id():
    with pytest.raises(ValueError, match="strategy_id and experiment_id"):
        handler.run_archive(None, {"job_type": "ARCHIVE", "strategy_id": None, "experiment_id": None, "payload": {}})


def test_run_archive_requires_a_notebook_in_the_response(conn, strategy_id, experiment_id):
    handler.set_session(StubKnowledgeSession(_knowledge(notebook=None)))
    with pytest.raises(ValueError, match="lab_notebooks draft"):
        handler.run_archive(conn, _archive_job(strategy_id, experiment_id))


# -- idempotency: A5 runs once per strategy (TRD §12.1) -----------------------


def test_a_second_archive_for_the_same_strategy_is_a_stale_no_op(conn, strategy_id, experiment_id):
    """No session installed — if a stale job ever tried to call one, this
    would crash on the lazy `AnthropicSession` import."""
    LabNotebookRepository(conn).insert(
        strategy_id=strategy_id, hypothesis="h", result="r", reason="re", evidence="e", next_questions=["q"]
    )
    outcome = handler.run_archive(conn, _archive_job(strategy_id, experiment_id))
    assert outcome.stale is True

    with transaction(conn, immediate=True):
        result = handler.persist_archive(conn, {}, outcome)
    assert result.tokens_spent == 0
    assert len(LabNotebookRepository(conn).for_strategy(strategy_id)) == 1  # still just the one


# -- the full write: notebook + entries + edges + questions + terminal state --


def test_archive_writes_everything_and_closes_every_experiment(conn, strategy_id, experiment_id):
    handler.set_session(StubKnowledgeSession(_knowledge()))
    job = _archive_job(strategy_id, experiment_id)

    outcome = handler.run_archive(conn, job)
    assert outcome.stale is False
    with transaction(conn, immediate=True):
        result = handler.persist_archive(conn, job, outcome)
    assert result.tokens_spent == 0

    notebooks = LabNotebookRepository(conn).for_strategy(strategy_id)
    assert len(notebooks) == 1
    assert notebooks[0]["next_questions"] == ["try a different filter family"]
    assert notebooks[0]["rendered_markdown"] is not None

    entries = KnowledgeEntryRepository(conn).find(strategy_id=strategy_id)
    assert len(entries) == 1
    assert entries[0]["evidence"]["failure_reasons"] == ["plateaued_below_bar"]
    assert entries[0]["future_ideas"] == ["idea"]

    edges = KnowledgeEdgeRepository(conn).find(subject="momentum", predicate="fails_in", object="sideways")
    assert len(edges) == 1
    assert edges[0]["evidence_count"] == 1

    questions = ResearchQuestionRepository(conn).find(status="open")
    assert len(questions) == 1
    assert questions[0]["origin_type"] == "experiment_failure"

    experiment = ExperimentRepository(conn).get(experiment_id)
    assert experiment["status"] == "archived"


def test_archive_supersedes_when_the_response_says_so(conn, strategy_id, experiment_id):
    old_id = KnowledgeEntryRepository(conn).record(
        entry_type="lesson", scope="global", title="old", statement="s", future_ideas=["x"]
    )
    handler.set_session(
        StubKnowledgeSession(
            _knowledge(
                entries=[
                    KnowledgeEntryDraft(
                        entry_type="lesson", scope="global", title="new", statement="s2",
                        future_ideas=["y"], supersedes_existing_id=old_id,
                    )
                ]
            )
        )
    )
    job = _archive_job(strategy_id, experiment_id)
    outcome = handler.run_archive(conn, job)
    with transaction(conn, immediate=True):
        handler.persist_archive(conn, job, outcome)

    old_row = KnowledgeEntryRepository(conn).get(old_id)
    assert old_row["superseded_by"] is not None


def test_archive_emits_nothing_directly_but_experiments_reach_archived(conn, strategy_id, experiment_id):
    """A5 is a terminal step — no follow-on job (PROMOTE/ARCHIVE emit ARCHIVE,
    but ARCHIVE itself emits nothing further)."""
    handler.set_session(StubKnowledgeSession(_knowledge()))
    job = _archive_job(strategy_id, experiment_id)
    outcome = handler.run_archive(conn, job)
    with transaction(conn, immediate=True):
        handler.persist_archive(conn, job, outcome)

    assert JobRepository(conn).find(job_type="ARCHIVE") == []
    assert JobRepository(conn).find(job_type="PROMOTE") == []


# -- MINE_PATTERNS: cross-experiment, weekly -----------------------------------


def _mine_job() -> dict:
    return {"job_type": "MINE_PATTERNS", "strategy_id": None, "experiment_id": None, "payload": {}}


def _close_experiment(conn, strategy_id: int, spec_id: int, snapshot_id: int, *, failure_reason: str) -> int:
    experiments = ExperimentRepository(conn)
    iteration = experiments.next_iteration(strategy_id)
    exp_id = experiments.start(
        strategy_id, iteration, spec_id=spec_id, data_snapshot_id=snapshot_id, code_commit="deadbeef"
    )
    experiments.complete(exp_id, status="evaluated", outcome="failed", failure_reason=failure_reason)
    return exp_id


def test_mine_groups_recent_failures_by_family(conn, strategy_id, spec_id, snapshot_id):
    _close_experiment(conn, strategy_id, spec_id, snapshot_id, failure_reason="overfit_in_sample")
    _close_experiment(conn, strategy_id, spec_id, snapshot_id, failure_reason="look_ahead_detected")  # excluded — a bug
    handler.set_session(StubKnowledgeSession(_knowledge(notebook=None)))

    outcome = handler.run_mine(conn, _mine_job())
    family = StrategyRepository(conn).get(strategy_id)["family"]
    assert family in outcome.family_groups
    reasons = [row["failure_reason"] for row in outcome.family_groups[family]]
    assert reasons == ["overfit_in_sample"]  # the bug-category experiment never enters the pool


def test_mine_writes_family_scoped_entries_and_edges_no_notebook(conn, strategy_id, spec_id, snapshot_id):
    _close_experiment(conn, strategy_id, spec_id, snapshot_id, failure_reason="overfit_in_sample")
    handler.set_session(StubKnowledgeSession(_knowledge(notebook=None)))

    outcome = handler.run_mine(conn, _mine_job())
    with transaction(conn, immediate=True):
        result = handler.persist_mine(conn, _mine_job(), outcome)
    assert result.tokens_spent == 0

    entries = KnowledgeEntryRepository(conn).find(scope="family")
    assert len(entries) == 1
    assert entries[0]["strategy_id"] is None  # cross-experiment — no single strategy

    edges = KnowledgeEdgeRepository(conn).find(subject="momentum", predicate="fails_in", object="sideways")
    assert len(edges) == 1

    questions = ResearchQuestionRepository(conn).find(status="open")
    assert len(questions) == 1
    assert questions[0]["origin_type"] == "pattern_detection"


def test_mine_with_nothing_to_report_writes_nothing(conn):
    """Finding no pattern in an empty batch is a correct result, not a
    failure (module docstring's own note)."""
    handler.set_session(StubKnowledgeSession(ProposedKnowledge()))
    outcome = handler.run_mine(conn, _mine_job())
    with transaction(conn, immediate=True):
        result = handler.persist_mine(conn, _mine_job(), outcome)
    assert result.tokens_spent == 0
    assert KnowledgeEntryRepository(conn).find() == []

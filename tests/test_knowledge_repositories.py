"""Stage 8's write path into the knowledge base (Implementation_Plan §11):
`KnowledgeEdgeRepository`'s upsert-not-duplicate contract, the mandatory-
non-empty guards `KnowledgeEntryRepository.record`/`LabNotebookRepository`
enforce, and the `repeat_failure_rate` metric §11's done-when names.
"""
from __future__ import annotations

import pytest

from aqrl.db.repositories import (
    ExperimentRepository,
    KnowledgeEdgeRepository,
    KnowledgeEntryRepository,
    LabNotebookRepository,
    SpecRepository,
    StrategyRepository,
    repeat_failure_rate,
)
from aqrl.operators.spec import Node, StrategySpec

MARKET = "nse_equity"
TIMEFRAME = "daily"


def _spec(hypothesis: str = "h") -> StrategySpec:
    return StrategySpec(
        entry_logic=[Node(id="e", operator="ema", inputs={"series": "price.close"}, params={"span": 5})],
        hypothesis=hypothesis,
    )


# -- KnowledgeEdgeRepository ----------------------------------------------------


def test_observe_refuses_an_edge_with_no_experiment_backing(conn):
    with pytest.raises(ValueError, match="no.*backing experiments|backing"):
        KnowledgeEdgeRepository(conn).observe("momentum", "fails_in", "sideways", experiment_ids=[])


def test_observe_inserts_on_first_sight(conn):
    edges = KnowledgeEdgeRepository(conn)
    edge_id = edges.observe("momentum", "works_in", "trending", experiment_ids=[1, 2], confidence=0.6)
    row = edges.find(subject="momentum", predicate="works_in", object="trending")[0]
    assert row["id"] == edge_id
    assert row["evidence_count"] == 1
    assert row["counter_evidence_count"] == 0
    assert sorted(row["supporting_experiments"]) == [1, 2]


def test_observe_repeated_sight_updates_counts_never_duplicates(conn):
    edges = KnowledgeEdgeRepository(conn)
    first_id = edges.observe("momentum", "works_in", "trending", experiment_ids=[1])
    second_id = edges.observe("momentum", "works_in", "trending", experiment_ids=[2, 3], confidence=0.8)

    assert first_id == second_id
    rows = edges.find(subject="momentum", predicate="works_in", object="trending")
    assert len(rows) == 1  # never a duplicate row — UNIQUE(subject, predicate, object)
    assert rows[0]["evidence_count"] == 2
    assert sorted(rows[0]["supporting_experiments"]) == [1, 2, 3]
    assert rows[0]["confidence"] == 0.8


def test_observe_a_contradicting_sight_increments_counter_evidence(conn):
    edges = KnowledgeEdgeRepository(conn)
    edges.observe("momentum", "works_in", "trending", experiment_ids=[1])
    edges.observe("momentum", "works_in", "trending", experiment_ids=[2], supports=False)

    row = edges.find(subject="momentum", predicate="works_in", object="trending")[0]
    assert row["evidence_count"] == 1
    assert row["counter_evidence_count"] == 1


# -- mandatory non-empty guards --------------------------------------------------


def test_knowledge_entry_record_rejects_empty_future_ideas(conn):
    with pytest.raises(ValueError, match="future_ideas"):
        KnowledgeEntryRepository(conn).record(
            entry_type="lesson", scope="global", title="t", statement="s", future_ideas=[]
        )


def test_knowledge_entry_record_accepts_non_empty_future_ideas(conn):
    entry_id = KnowledgeEntryRepository(conn).record(
        entry_type="lesson", scope="global", title="t", statement="s", future_ideas=["try x"]
    )
    assert entry_id > 0


def test_lab_notebook_rejects_empty_next_questions(conn):
    with pytest.raises(ValueError, match="next_questions"):
        LabNotebookRepository(conn).insert(hypothesis="h", result="r", reason="re", evidence="ev", next_questions=[])


def test_knowledge_entry_supersede_links_without_deleting(conn):
    entries = KnowledgeEntryRepository(conn)
    old_id = entries.record(entry_type="lesson", scope="global", title="old", statement="s", future_ideas=["x"])
    new_id = entries.record(entry_type="lesson", scope="global", title="new", statement="s2", future_ideas=["y"])
    entries.supersede(old_id, new_id)

    old_row = entries.get(old_id)
    assert old_row["superseded_by"] == new_id
    assert entries.get(new_id) is not None  # both rows still exist


# -- repeat_failure_rate ----------------------------------------------------------


@pytest.fixture
def strategy_id(conn) -> int:
    return StrategyRepository(conn).insert(name="s", family="fam", market=MARKET, timeframe=TIMEFRAME)


def _experiment_with_spec(conn, strategy_id: int, iteration: int, *, spec_created_at: str, failure_reason: str) -> int:
    spec_id = SpecRepository(conn).insert_spec(
        _spec(f"h-{iteration}"), strategy_id, version=iteration, created_at=spec_created_at
    )
    experiments = ExperimentRepository(conn)
    experiment_id = experiments.insert(strategy_id=strategy_id, iteration=iteration, spec_id=spec_id, status="evaluated")
    experiments.update(experiment_id, failure_reason=failure_reason, outcome="failed")
    return experiment_id


def test_repeat_failure_rate_with_no_experiments_is_zero(conn):
    result = repeat_failure_rate(conn)
    assert result == {"repeats": 0, "total": 0, "rate": 0.0}


def test_repeat_failure_rate_counts_a_documented_repeat(conn, strategy_id):
    # The lesson exists first...
    KnowledgeEntryRepository(conn).record(
        entry_type="lesson",
        scope="family",
        title="ATR overfits",
        statement="ATR > 3.0 consistently overfits",
        applicable_markets=[MARKET],
        applicable_timeframes=[TIMEFRAME],
        evidence={"experiment_ids": [1], "failure_reasons": ["overfit_in_sample"]},
        future_ideas=["normalise ATR"],
        created_at="2026-01-01T00:00:00+00:00",
    )
    # ...and this experiment's spec was proposed after — a preventable repeat.
    _experiment_with_spec(
        conn, strategy_id, 1, spec_created_at="2026-02-01T00:00:00+00:00", failure_reason="overfit_in_sample"
    )

    result = repeat_failure_rate(conn)
    assert result == {"repeats": 1, "total": 1, "rate": 1.0}


def test_repeat_failure_rate_ignores_a_lesson_written_after_the_spec(conn, strategy_id):
    # The experiment's spec predates the lesson — A1 could not have known.
    _experiment_with_spec(
        conn, strategy_id, 1, spec_created_at="2026-01-01T00:00:00+00:00", failure_reason="overfit_in_sample"
    )
    KnowledgeEntryRepository(conn).record(
        entry_type="lesson",
        scope="family",
        title="ATR overfits",
        statement="ATR > 3.0 consistently overfits",
        applicable_markets=[MARKET],
        applicable_timeframes=[TIMEFRAME],
        evidence={"experiment_ids": [1], "failure_reasons": ["overfit_in_sample"]},
        future_ideas=["normalise ATR"],
        created_at="2026-02-01T00:00:00+00:00",
    )

    result = repeat_failure_rate(conn)
    assert result == {"repeats": 0, "total": 1, "rate": 0.0}


def test_repeat_failure_rate_ignores_bug_failure_reasons(conn, strategy_id):
    KnowledgeEntryRepository(conn).record(
        entry_type="lesson",
        scope="global",
        title="whatever",
        statement="s",
        evidence={"experiment_ids": [1], "failure_reasons": ["look_ahead_detected"]},
        future_ideas=["x"],
        created_at="2026-01-01T00:00:00+00:00",
    )
    _experiment_with_spec(
        conn, strategy_id, 1, spec_created_at="2026-02-01T00:00:00+00:00", failure_reason="look_ahead_detected"
    )

    result = repeat_failure_rate(conn)
    assert result == {"repeats": 0, "total": 0, "rate": 0.0}

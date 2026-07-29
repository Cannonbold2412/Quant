"""`events.emit` — the TRD §4.1 event -> job_type table."""
from __future__ import annotations

import pytest

from aqrl.db import transaction
from aqrl.db.repositories import AuditLogRepository, ExperimentRepository, JobRepository, StrategyRepository
from aqrl.orchestration.events import EVENT_JOB_TYPE, Event, UnknownEvent, emit


@pytest.fixture
def strategy_id(conn) -> int:
    return StrategyRepository(conn).insert(name="n", family="f", market="m", timeframe="t")


@pytest.fixture
def experiment_id(conn, strategy_id: int) -> int:
    return ExperimentRepository(conn).start(strategy_id, 1)


def test_every_trd_4_1_event_is_declared():
    """TRD §4.1, transcribed. A missing entry here is a missing entry there."""
    expected = {
        Event.SPEC_SAVED: "IMPLEMENT",
        Event.CODE_CHECKS_PASSED: "EVALUATE",
        Event.CODE_CHECKS_FAILED: "FIX_CODE",
        Event.EVALUATION_CLEARED_BAR: "PROMOTE",
        Event.EVALUATION_FAILED_BAR: "REVIEW",
        Event.REVIEW_ITERATE: "IMPLEMENT",
        Event.REVIEW_PLATEAU_OR_REJECT: "ARCHIVE",
        Event.PROMOTION_DECIDED: "ARCHIVE",
        Event.PAPER_TRADING_MILESTONE: "MONITOR_DEPLOYMENT",
        Event.DOCUMENT_INGESTED: "EXTRACT_KNOWLEDGE",
        Event.HIGH_NOVELTY_EXTRACTION: "GENERATE_SPEC",
    }
    for event, job_type in expected.items():
        assert EVENT_JOB_TYPE[event] == job_type

    # Direct actions, not queued jobs (see events.py's module docstring).
    for event in (
        Event.HUMAN_APPROVED_GATE,
        Event.HEALTH_CHECK_RED,
        Event.FAILURE_PATTERN_DETECTED,
        Event.DATA_SNAPSHOT_FLAGGED,
    ):
        assert EVENT_JOB_TYPE[event] is None

    assert set(EVENT_JOB_TYPE) == set(Event)


def test_emit_enqueues_the_mapped_job_type(conn, strategy_id, experiment_id):
    with transaction(conn, immediate=True):
        job_id = emit(conn, Event.EVALUATION_CLEARED_BAR, strategy_id=strategy_id, experiment_id=experiment_id)
    job = JobRepository(conn).get(job_id)
    assert job["job_type"] == "PROMOTE"
    assert job["strategy_id"] == strategy_id
    assert job["experiment_id"] == experiment_id


def test_emit_writes_an_audit_row_even_for_a_jobless_event(conn):
    with transaction(conn, immediate=True):
        result = emit(conn, Event.HEALTH_CHECK_RED, strategy_id=5, reasoning="drawdown exceeded expected")
    assert result is None
    audit = AuditLogRepository(conn).for_entity("strategies", 5)
    assert len(audit) == 1
    assert audit[0]["action"] == "event:health_check_red"
    assert audit[0]["reasoning"] == "drawdown exceeded expected"


def test_emit_default_dedupe_key_prevents_double_fire(conn, strategy_id, experiment_id):
    with transaction(conn, immediate=True):
        first = emit(conn, Event.EVALUATION_FAILED_BAR, strategy_id=strategy_id, experiment_id=experiment_id)
    with transaction(conn, immediate=True):
        second = emit(conn, Event.EVALUATION_FAILED_BAR, strategy_id=strategy_id, experiment_id=experiment_id)
    assert first == second
    assert JobRepository(conn).count(job_type="REVIEW") == 1


def test_emit_rejects_unregistered_events(conn):
    with pytest.raises(UnknownEvent):
        with transaction(conn, immediate=True):
            emit(conn, "not_a_real_event", strategy_id=1)

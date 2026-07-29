"""The state machines — invalid transitions are errors, not warnings
(Implementation_Plan §6)."""
from __future__ import annotations

import pytest

from aqrl.db.repositories import AuditLogRepository, ExperimentRepository, StrategyRepository
from aqrl.orchestration.states import (
    EXPERIMENT_TRANSITIONS,
    STRATEGY_TRANSITIONS,
    InvalidTransition,
    transition,
)


@pytest.fixture
def strategy_id(conn) -> int:
    return StrategyRepository(conn).insert(name="n", family="f", market="m", timeframe="t", status="draft")


def test_valid_transition_applies_and_audits(conn, strategy_id):
    transition(conn, "strategies", strategy_id, "spec_ready", actor="test", reasoning="spec written")
    row = StrategyRepository(conn).get(strategy_id)
    assert row["status"] == "spec_ready"

    audit = AuditLogRepository(conn).for_entity("strategies", strategy_id)
    assert len(audit) == 1
    assert audit[0]["reasoning"] == "spec written"
    assert "draft" in audit[0]["action"] and "spec_ready" in audit[0]["action"]


def test_invalid_transition_raises_and_does_not_write(conn, strategy_id):
    with pytest.raises(InvalidTransition):
        transition(conn, "strategies", strategy_id, "live_scaled")

    row = StrategyRepository(conn).get(strategy_id)
    assert row["status"] == "draft"
    assert AuditLogRepository(conn).for_entity("strategies", strategy_id) == []


def test_transition_rejects_unknown_table(conn, strategy_id):
    with pytest.raises(ValueError):
        transition(conn, "not_a_table", strategy_id, "anything")


def test_transition_rejects_missing_row(conn):
    with pytest.raises(KeyError):
        transition(conn, "strategies", 999_999, "spec_ready")


def test_transition_can_set_extra_fields_atomically(conn, strategy_id):
    transition(conn, "strategies", strategy_id, "spec_ready")
    conn.execute("UPDATE strategies SET status = 'coding' WHERE id = ?", (strategy_id,))
    conn.execute("UPDATE strategies SET status = 'evaluating' WHERE id = ?", (strategy_id,))

    transition(
        conn, "strategies", strategy_id, "quarantined",
        quarantined=True, quarantine_reason="3 consecutive failures",
    )
    row = StrategyRepository(conn).get(strategy_id)
    assert row["status"] == "quarantined"
    assert row["quarantined"] == 1
    assert row["quarantine_reason"] == "3 consecutive failures"


def test_experiment_transitions_cover_the_schema_enum(conn):
    strategy_id = StrategyRepository(conn).insert(name="n2", family="f", market="m", timeframe="t")
    experiment_id = ExperimentRepository(conn).start(strategy_id, 1)  # starts 'evaluating'

    transition(conn, "experiments", experiment_id, "evaluated")
    transition(conn, "experiments", experiment_id, "archived")
    with pytest.raises(InvalidTransition):
        transition(conn, "experiments", experiment_id, "evaluated")


@pytest.mark.parametrize("table, transitions", [("strategies", STRATEGY_TRANSITIONS), ("experiments", EXPERIMENT_TRANSITIONS)])
def test_every_declared_target_is_a_real_status(table, transitions):
    """A typo in the transition table would otherwise only surface the first
    time some future stage tried to use it."""
    all_states = set(transitions)
    for targets in transitions.values():
        assert targets <= all_states, f"{table} transition table references an undeclared status"

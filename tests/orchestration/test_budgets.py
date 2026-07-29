"""Back-pressure over the `budgets` table (TRD §4.4)."""
from __future__ import annotations

from aqrl.orchestration.budgets import BudgetRepository, check, check_global, consume


def test_unconfigured_budget_is_unlimited(conn):
    result = check(conn, "global", "tokens", "day")
    assert result.allowed


def test_consume_without_a_configured_budget_is_a_noop(conn):
    consume(conn, "global", "tokens", "day", 1000)  # nothing to raise against
    assert BudgetRepository(conn).count() == 0


def test_consume_trips_exhausted_at_the_limit(conn):
    BudgetRepository(conn).upsert("global", "experiments", "day", 2)
    consume(conn, "global", "experiments", "day", 1)
    assert check(conn, "global", "experiments", "day").allowed

    consume(conn, "global", "experiments", "day", 1)
    result = check(conn, "global", "experiments", "day")
    assert not result.allowed
    assert "budget_exhausted" in result.reason


def test_check_global_reports_the_first_exhausted_cap(conn):
    BudgetRepository(conn).upsert("global", "experiments", "day", 1)
    consume(conn, "global", "experiments", "day", 1)
    result = check_global(conn)
    assert not result.allowed
    assert "experiments" in result.reason


def test_lifetime_budget_never_rolls(conn):
    BudgetRepository(conn).upsert("strategy", "iterations", "lifetime", 1, scope_id=7)
    consume(conn, "strategy", "iterations", "lifetime", 1, scope_id=7)
    assert not check(conn, "strategy", "iterations", "lifetime", scope_id=7).allowed


def test_daily_budget_rolls_over_on_a_new_day(conn):
    budgets = BudgetRepository(conn)
    row_id = budgets.upsert("global", "tokens", "day", 10)
    budgets.update(row_id, used_value=10, exhausted=True, period_start="2000-01-01T00:00:00+00:00")

    result = check(conn, "global", "tokens", "day")
    assert result.allowed, "a period_start far in the past must roll before the check"
    row = budgets.get(row_id)
    assert row["used_value"] == 0
    assert row["exhausted"] == 0


def test_upsert_is_idempotent_on_scope_and_re_limits(conn):
    budgets = BudgetRepository(conn)
    first = budgets.upsert("global", "tokens", "day", 100)
    second = budgets.upsert("global", "tokens", "day", 200)
    assert first == second
    assert budgets.get(first)["limit_value"] == 200
    assert budgets.count() == 1


def test_scope_id_isolates_budgets(conn):
    budgets = BudgetRepository(conn)
    budgets.upsert("strategy", "tokens", "lifetime", 5, scope_id=1)
    budgets.upsert("strategy", "tokens", "lifetime", 5, scope_id=2)
    consume(conn, "strategy", "tokens", "lifetime", 5, scope_id=1)

    assert not check(conn, "strategy", "tokens", "lifetime", scope_id=1).allowed
    assert check(conn, "strategy", "tokens", "lifetime", scope_id=2).allowed

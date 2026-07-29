"""Persistence — the operator table, spec storage, and duplicate rejection.

The load-bearing behaviour here is `insert_spec` refusing an exact re-run
**before compute is spent** (App-Flow §3.4). `strategy_specs.spec_hash` is
UNIQUE so the database would refuse it anyway, but only with an opaque
IntegrityError after a worker had already claimed a job. Catching it here, with
the prior spec attached, is what lets A1 be told *"you already tried this, and
here is what happened"* instead of merely being blocked.
"""
from __future__ import annotations

import pytest

from aqrl.db.repositories import (
    DuplicateSpecError,
    OperatorRepository,
    SpecOperatorRepository,
    SpecRepository,
    StrategyRepository,
)
from aqrl.operators import Node, StrategySpec, all_operators, operator_library_version


@pytest.fixture
def strategy_id(conn) -> int:
    return StrategyRepository(conn).get_or_create(
        name="dual-ma", family="trend_following", market="nse_equity", timeframe="daily"
    )


def dual_ma(fast: int = 20, slow: int = 100) -> StrategySpec:
    return StrategySpec(
        entry_logic=[
            Node(id="fast", operator="rolling_mean", params={"window": fast},
                 inputs={"series": "price.close"}),
            Node(id="slow", operator="rolling_mean", params={"window": slow},
                 inputs={"series": "price.close"}),
            Node(id="cross", operator="crossover", params={"min_gap_pct": 0.002},
                 inputs={"fast": "fast", "slow": "slow"}),
        ],
        hypothesis="Trend persists at a 20/100-bar horizon.",
    )


# -- the operator table -----------------------------------------------------------


def test_sync_mirrors_the_whole_registry(conn):
    counts = OperatorRepository(conn).sync()
    assert counts["inserted"] == len(all_operators())
    assert OperatorRepository(conn).count() == len(all_operators())


def test_sync_is_idempotent(conn):
    repo = OperatorRepository(conn)
    repo.sync()
    second = repo.sync()
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == len(all_operators())


def test_synced_rows_carry_the_declaration(conn):
    repo = OperatorRepository(conn)
    repo.sync()
    row = repo.by_name("crossover", "1.0.0")
    assert row["category"] == "signal"
    assert row["test_status"] == "tested"
    assert row["implementation_ref"].endswith("Crossover")
    assert [spec["name"] for spec in row["parameters"]] == ["min_gap_pct"]


def test_the_library_is_not_self_modifying(conn):
    """`approved_by` stays NULL until a person signs off (TRD §11.1)."""
    repo = OperatorRepository(conn)
    repo.sync()
    assert all(row["approved_by"] is None for row in repo.find())

    repo.approve("crossover", "1.0.0", "a-human")
    assert repo.by_name("crossover", "1.0.0")["approved_by"] == "a-human"


def test_approval_requires_a_named_person(conn):
    OperatorRepository(conn).sync()
    with pytest.raises(ValueError, match="not self-modifying"):
        OperatorRepository(conn).approve("crossover", "1.0.0", "")


def test_approving_an_unknown_operator_fails(conn):
    OperatorRepository(conn).sync()
    with pytest.raises(LookupError):
        OperatorRepository(conn).approve("no_such_operator", None, "a-human")


def test_the_library_version_is_available_for_stamping(conn):
    assert OperatorRepository(conn).library_version() == operator_library_version()


# -- spec storage ------------------------------------------------------------------


def test_inserting_a_spec_stores_its_hash(conn, strategy_id):
    repo = SpecRepository(conn)
    spec = dual_ma()
    spec_id = repo.insert_spec(spec, strategy_id)

    row = repo.get(spec_id)
    assert row["spec_hash"] == spec.spec_hash()
    assert row["version"] == 1
    assert row["hypothesis"] == spec.hypothesis
    assert [node["id"] for node in row["entry_logic"]] == ["fast", "slow", "cross"]


def test_an_exact_duplicate_is_rejected_before_compute(conn, strategy_id):
    repo = SpecRepository(conn)
    first = repo.insert_spec(dual_ma(), strategy_id)

    with pytest.raises(DuplicateSpecError) as caught:
        repo.insert_spec(dual_ma(), strategy_id)

    # The prior row travels with the error, so the caller can attach its result.
    assert caught.value.existing["id"] == first
    assert caught.value.spec_hash == dual_ma().spec_hash()


def test_a_cosmetically_different_duplicate_is_also_rejected(conn, strategy_id):
    """The case that makes structural hashing worth the effort.

    Renamed nodes and a reworded hypothesis describe the same computation, so
    they must not buy a second run — and a phantom trial in the family count.
    """
    repo = SpecRepository(conn)
    repo.insert_spec(dual_ma(), strategy_id)

    disguised = StrategySpec(
        entry_logic=[
            Node(id="f", operator="rolling_mean", params={"window": 20},
                 inputs={"series": "price.close"}),
            Node(id="s", operator="rolling_mean", params={"window": 100},
                 inputs={"series": "price.close"}),
            Node(id="x", operator="crossover", params={"min_gap_pct": 0.002},
                 inputs={"slow": "s", "fast": "f"}),
        ],
        hypothesis="A completely different sentence.",
    )
    with pytest.raises(DuplicateSpecError):
        repo.insert_spec(disguised, strategy_id)


def test_a_genuinely_different_spec_is_accepted(conn, strategy_id):
    repo = SpecRepository(conn)
    repo.insert_spec(dual_ma(20, 100), strategy_id)
    second = repo.insert_spec(dual_ma(20, 150), strategy_id)
    assert repo.get(second)["version"] == 2


def test_an_invalid_spec_fails_before_it_reaches_the_database(conn, strategy_id):
    from aqrl.operators import SpecError

    broken = StrategySpec(
        entry_logic=[Node(id="a", operator="rolling_mean", params={"window": 1},
                          inputs={"series": "price.close"})]
    )
    with pytest.raises(SpecError):
        SpecRepository(conn).insert_spec(broken, strategy_id)
    assert SpecRepository(conn).count() == 0


# -- the operator-usage join --------------------------------------------------------


def test_operator_usage_is_recorded_transitively(conn, strategy_id):
    """Backend-Schema §15 Q4: *"which experiments ever used a Kalman filter?"*

    Transitive — a spec whose entry root is a crossover fed by two rolling means
    used all three, and a query for `rolling_mean` must find it.
    """
    repo = SpecRepository(conn)
    repo.insert_spec(dual_ma(), strategy_id)

    assert len(repo.using_operator("crossover")) == 1
    assert len(repo.using_operator("rolling_mean")) == 1
    assert repo.using_operator("kalman") == []


def test_usage_rows_carry_the_role_and_bound_parameters(conn, strategy_id):
    spec_id = SpecRepository(conn).insert_spec(dual_ma(), strategy_id)
    rows = SpecOperatorRepository(conn).for_spec(spec_id)

    assert {row["role"] for row in rows} == {"entry"}
    windows = sorted(
        row["parameters_used"]["window"] for row in rows if "window" in row["parameters_used"]
    )
    assert windows == [20, 100]


def test_usage_is_recorded_per_role(conn, strategy_id):
    spec = StrategySpec(
        entry_logic=dual_ma().entry_logic,
        risk_logic=[
            Node(id="stop", operator="atr_stop", params={"period": 14, "multiple": 2.0},
                 inputs={"close": "price.close", "high": "price.high", "low": "price.low"})
        ],
    )
    spec_id = SpecRepository(conn).insert_spec(spec, strategy_id)

    roles = {row["role"] for row in SpecOperatorRepository(conn).for_spec(spec_id)}
    assert roles == {"entry", "risk"}


def test_the_join_populates_operators_on_demand(conn, strategy_id):
    """A spec inserted before `operators sync` must still record provenance."""
    assert OperatorRepository(conn).count() == 0
    SpecRepository(conn).insert_spec(dual_ma(), strategy_id)
    assert OperatorRepository(conn).count() == len(all_operators())
    assert len(SpecRepository(conn).using_operator("rolling_mean")) == 1

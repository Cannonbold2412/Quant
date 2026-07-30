"""The `IMPLEMENT` / `FIX_CODE` handler (Stage 5 — Implementation_Plan §8).

Stage 5's actual done-when: *"given a hand-written spec, A2 produces code
that passes P0 and runs through `evaluate.py` unattended."* Exercised here
first through direct `run()`/`persist()` calls (fast, precise), then once
through the real job queue and a real worker subprocess (the thing that
actually has to be true).
"""
from __future__ import annotations

import pytest

from aqrl.agents.session import ProposedSpec, StubSession
from aqrl.db import transaction
from aqrl.db.repositories import (
    CodeVersionRepository,
    ExperimentRepository,
    JobRepository,
    ResearchPlanRepository,
    SpecRepository,
    StrategyRepository,
)
from aqrl.db.repositories.operators import DuplicateSpecError
from aqrl.orchestration import states
from aqrl.orchestration.events import Event, emit
from aqrl.orchestration.handlers import implement as handler
from aqrl.orchestration.worker import run_job

from .conftest import ASSET_CLASS, MARKET, TIMEFRAME, crossover_spec


@pytest.fixture(autouse=True)
def _reset_session(monkeypatch):
    """Every test starts with the default (lazy `AnthropicSession`) unless it
    explicitly installs a stub — no test may leak a session into another."""
    handler.set_session(None)
    yield
    handler.set_session(None)


@pytest.fixture
def strategy_id(conn) -> int:
    return StrategyRepository(conn).insert(
        name="a2-fixture", family="a2-fixture-family", market=MARKET, timeframe=TIMEFRAME, status="draft"
    )


@pytest.fixture
def spec_id(conn, strategy_id: int) -> int:
    return SpecRepository(conn).insert_spec(crossover_spec(), strategy_id)


def _new_implement_job(conn, strategy_id: int, spec_id: int, **payload_overrides) -> dict:
    payload = {"asset_class": ASSET_CLASS, "spec_id": spec_id, **payload_overrides}
    with transaction(conn, immediate=True):
        states.transition(conn, "strategies", strategy_id, "spec_ready", actor="test")
        job_id = emit(conn, Event.SPEC_SAVED, strategy_id=strategy_id, payload=payload)
    return JobRepository(conn).get(job_id)


def _bad_spec(offset: float = 0.0) -> ProposedSpec:
    """A structurally invalid proposal — an absolute price threshold, the
    exact TRD §14.2c violation the sandbox's `no_absolute_price_levels`
    check exists to catch. `offset` varies the spec_hash across calls, so
    repeated FIX_CODE attempts don't collide with `DuplicateSpecError`."""
    return ProposedSpec(
        entry_logic=[
            {
                "id": "e1",
                "operator": "threshold",
                "params": {"upper": 500.0 + offset, "lower": -500.0},
                "inputs": {"series": "price.close"},
            }
        ],
        hypothesis=f"bad-{offset}",
        change_summary=f"deliberately bad attempt {offset}",
    )


# -- run(): validation --------------------------------------------------------


def test_run_requires_strategy_id():
    with pytest.raises(ValueError, match="strategy_id"):
        handler.run(None, {"job_type": "IMPLEMENT", "strategy_id": None, "payload": {}})


def test_run_requires_asset_class(conn, strategy_id):
    job = {"job_type": "IMPLEMENT", "strategy_id": strategy_id, "experiment_id": None, "payload": {}}
    with pytest.raises(ValueError, match="asset_class"):
        handler.run(conn, job)


def test_run_requires_spec_id_or_research_plan_id(conn, strategy_id):
    job = {
        "job_type": "IMPLEMENT",
        "strategy_id": strategy_id,
        "experiment_id": None,
        "payload": {"asset_class": ASSET_CLASS},
    }
    with pytest.raises(ValueError, match="spec_id"):
        handler.run(conn, job)


def test_fix_code_requires_experiment_id(conn, strategy_id):
    job = {
        "job_type": "FIX_CODE",
        "strategy_id": strategy_id,
        "experiment_id": None,
        "payload": {"asset_class": ASSET_CLASS},
    }
    with pytest.raises(ValueError, match="experiment_id"):
        handler.run(conn, job)


# -- first iteration: the hand-written-spec path, no LLM call ------------------


def test_first_iteration_never_touches_a_session(conn, strategy_id, spec_id):
    """No `AgentSession` installed at all — if this path ever tried to call
    one, the test would crash on `anthropic` import / missing API key rather
    than silently succeeding, which is exactly the point."""
    job = _new_implement_job(conn, strategy_id, spec_id)
    outcome = handler.run(conn, job)
    assert outcome.mode == "first_iteration"
    assert outcome.tokens_spent == 0
    assert outcome.checks_passed is True


def test_first_iteration_persists_experiment_code_and_routes_to_evaluate(conn, strategy_id, spec_id):
    job = _new_implement_job(conn, strategy_id, spec_id, data_snapshot_id=99, cost_multiplier=3.0)
    outcome = handler.run(conn, job)
    with transaction(conn, immediate=True):
        result = handler.persist(conn, job, outcome)
    assert result.tokens_spent == 0

    strategies = StrategyRepository(conn)
    strategy = strategies.get(strategy_id)
    assert strategy["status"] == "evaluating"
    assert strategy["git_branch"] == f"strategy/{strategy['uid']}"
    assert strategy["code_path"] == f"strategies/{strategy['uid']}/strategy.py"

    experiments = ExperimentRepository(conn)
    experiment = experiments.find(strategy_id=strategy_id)[0]
    assert experiment["status"] == "evaluating"
    assert experiment["iteration"] == 1
    assert experiment["spec_id"] == spec_id
    assert experiment["code_version_id"] is not None

    code_version = CodeVersionRepository(conn).get(experiment["code_version_id"])
    assert code_version["compile_ok"] == 1
    assert code_version["git_commit"]

    evaluate_jobs = JobRepository(conn).find(job_type="EVALUATE")
    assert len(evaluate_jobs) == 1
    assert evaluate_jobs[0]["experiment_id"] == experiment["id"]
    # The eval-only payload carried through unchanged; handler-only keys stripped.
    assert evaluate_jobs[0]["payload"] == {
        "asset_class": ASSET_CLASS,
        "data_snapshot_id": 99,
        "cost_multiplier": 3.0,
    }


def test_running_the_same_job_twice_produces_one_commit(conn, strategy_id, spec_id):
    """`run()` is safe to retry — the git commit is content-addressed, so a
    crash-and-retry before `persist()` ever commits reproduces the same
    commit rather than a new one."""
    job = _new_implement_job(conn, strategy_id, spec_id)
    first = handler.run(conn, job)
    second = handler.run(conn, job)
    assert first.commit_hash == second.commit_hash
    assert first.code_hash == second.code_hash


# -- through the real queue + a real worker subprocess -------------------------


def test_first_iteration_runs_unattended_through_the_real_worker(conn, strategy_id, spec_id, snapshot_id):
    """The actual done-when: claim, run in a real subprocess, persist — no
    direct handler call, no LLM, nothing hand-fed beyond the spec."""
    job = _new_implement_job(conn, strategy_id, spec_id, data_snapshot_id=snapshot_id)
    jobs = JobRepository(conn)

    with transaction(conn, immediate=True):
        claimed = jobs.claim("w1", lease_seconds=120)
    assert claimed["id"] == job["id"]

    code = run_job(claimed["uid"], worker_id="w1")
    assert code == 0

    finished = jobs.get(job["id"])
    assert finished["status"] == "succeeded"

    evaluate_jobs = jobs.find(job_type="EVALUATE")
    assert len(evaluate_jobs) == 1

    with transaction(conn, immediate=True):
        eval_claimed = jobs.claim("w1", lease_seconds=120)
    assert eval_claimed["job_type"] == "EVALUATE"
    eval_code = run_job(eval_claimed["uid"], worker_id="w1")
    assert eval_code == 0
    assert jobs.get(eval_claimed["id"])["status"] == "succeeded"


# -- plan-driven iteration (Stage 6 stand-in) -----------------------------------


def _bar_failed_experiment(conn, strategy_id, spec_id):
    """Simulates the first iteration having already run and failed the bar —
    the state A3 (Stage 6, not built) would hand off from."""
    job = _new_implement_job(conn, strategy_id, spec_id)
    outcome = handler.run(conn, job)
    with transaction(conn, immediate=True):
        handler.persist(conn, job, outcome)
    experiment = ExperimentRepository(conn).find(strategy_id=strategy_id)[0]
    StrategyRepository(conn).update(strategy_id, status="iterating")
    plan_id = ResearchPlanRepository(conn).insert(
        experiment_id=experiment["id"],
        strategy_id=strategy_id,
        verdict="iterate",
        diagnosis="no signal on the synthetic fixture",
        evidence_cited={"note": "test fixture"},
        proposed_changes=[{"target": "entry", "change": "widen the gap", "reason": "reduce whipsaw"}],
        expected_effect="fewer false crossovers",
        confidence=0.4,
    )
    return experiment, plan_id


def test_plan_driven_iteration_opens_a_new_experiment(conn, strategy_id, spec_id):
    experiment, plan_id = _bar_failed_experiment(conn, strategy_id, spec_id)
    proposal = ProposedSpec(
        entry_logic=[
            {"id": "fast", "operator": "ema", "params": {"span": 5}, "inputs": {"series": "price.close"}},
            {"id": "slow", "operator": "ema", "params": {"span": 60}, "inputs": {"series": "price.close"}},
            {"id": "cross", "operator": "crossover", "inputs": {"fast": "fast", "slow": "slow"}},
        ],
        hypothesis="widened gap",
        change_summary="widened the EMA gap per the research plan",
    )
    handler.set_session(StubSession(proposal))

    # Simulate A3 (Stage 6): enqueue the follow-up IMPLEMENT by hand.
    with transaction(conn, immediate=True):
        job_id = emit(
            conn,
            Event.REVIEW_ITERATE,
            strategy_id=strategy_id,
            experiment_id=experiment["id"],
            payload={"research_plan_id": plan_id, "asset_class": ASSET_CLASS},
        )
    plan_job = JobRepository(conn).get(job_id)

    outcome = handler.run(conn, plan_job)
    assert outcome.mode == "plan_iteration"
    assert outcome.checks_passed is True
    assert outcome.change_summary == "widened the EMA gap per the research plan"

    with transaction(conn, immediate=True):
        handler.persist(conn, plan_job, outcome)

    experiments = ExperimentRepository(conn).find(strategy_id=strategy_id, order_by="iteration")
    assert len(experiments) == 2
    new_experiment = experiments[1]
    assert new_experiment["iteration"] == 2
    assert new_experiment["parent_experiment_id"] == experiment["id"]
    assert new_experiment["research_plan_id"] == plan_id
    assert new_experiment["spec_id"] != spec_id  # a new, distinct version

    new_spec = SpecRepository(conn).get(new_experiment["spec_id"])
    assert new_spec["version"] == 2
    assert new_spec["prompt_version"] is not None


# -- FIX_CODE: bounded retries, then quarantine --------------------------------


def test_fix_code_success_reuses_the_same_experiment(conn, strategy_id, spec_id):
    experiment, plan_id = _bar_failed_experiment(conn, strategy_id, spec_id)
    handler.set_session(StubSession(_bad_spec(0)))

    with transaction(conn, immediate=True):
        job_id = emit(
            conn, Event.REVIEW_ITERATE, strategy_id=strategy_id, experiment_id=experiment["id"],
            payload={"research_plan_id": plan_id, "asset_class": ASSET_CLASS},
        )
    plan_job = JobRepository(conn).get(job_id)
    outcome = handler.run(conn, plan_job)
    assert outcome.checks_passed is False
    with transaction(conn, immediate=True):
        handler.persist(conn, plan_job, outcome)

    new_experiment = ExperimentRepository(conn).find(strategy_id=strategy_id, order_by="iteration")[1]
    assert new_experiment["status"] == "code_pending"

    fix_job = JobRepository(conn).find(job_type="FIX_CODE")[0]
    assert fix_job["experiment_id"] == new_experiment["id"]
    assert fix_job["payload"]["fix_attempt"] == 1
    assert "absolute price level" in " ".join(fix_job["payload"]["diagnostics"])

    # This time the LLM gets it right.
    fixed = ProposedSpec(
        entry_logic=[
            {"id": "e", "operator": "ema", "params": {"span": 15}, "inputs": {"series": "price.close"}},
        ],
        hypothesis="fixed",
        change_summary="replaced the absolute threshold with a normal EMA signal",
    )
    handler.set_session(StubSession(fixed))
    fix_outcome = handler.run(conn, fix_job)
    assert fix_outcome.mode == "fix_code"
    assert fix_outcome.checks_passed is True
    with transaction(conn, immediate=True):
        handler.persist(conn, fix_job, fix_outcome)

    fixed_experiment = ExperimentRepository(conn).get(new_experiment["id"])
    assert fixed_experiment["status"] == "evaluating"
    assert fixed_experiment["spec_id"] != new_experiment["spec_id"]  # updated to the corrected spec

    # One EVALUATE from the first iteration's own success (`_bar_failed_experiment`
    # helper) plus one from this fix succeeding — never a second FIX_CODE.
    evaluate_jobs = JobRepository(conn).find(job_type="EVALUATE")
    assert len(evaluate_jobs) == 2
    assert {job["experiment_id"] for job in evaluate_jobs} == {experiment["id"], new_experiment["id"]}
    # Only the one FIX_CODE attempt ever existed — success never enqueues another.
    assert len(JobRepository(conn).find(job_type="FIX_CODE")) == 1

    versions = CodeVersionRepository(conn).for_experiment(new_experiment["id"])
    assert len(versions) == 2  # the bad attempt, then the fix — both recorded


def test_fix_code_quarantines_after_max_attempts(conn, strategy_id, spec_id, monkeypatch):
    monkeypatch.setenv("AQRL_MAX_FIX_ATTEMPTS", "2")
    from aqrl.config import reset_settings_cache

    reset_settings_cache()
    try:
        experiment, plan_id = _bar_failed_experiment(conn, strategy_id, spec_id)

        counter = iter(range(1000))

        def make_bad(brief: str) -> ProposedSpec:
            return _bad_spec(next(counter))

        handler.set_session(StubSession(make_bad))

        with transaction(conn, immediate=True):
            job_id = emit(
                conn, Event.REVIEW_ITERATE, strategy_id=strategy_id, experiment_id=experiment["id"],
                payload={"research_plan_id": plan_id, "asset_class": ASSET_CLASS},
            )
        job = JobRepository(conn).get(job_id)
        outcome = handler.run(conn, job)
        with transaction(conn, immediate=True):
            handler.persist(conn, job, outcome)

        new_experiment_id = ExperimentRepository(conn).find(strategy_id=strategy_id, order_by="iteration")[1]["id"]

        # max_fix_attempts=2: one more failing attempt must exhaust the budget.
        for _ in range(3):
            pending = JobRepository(conn).find(job_type="FIX_CODE", status="pending")
            if not pending:
                break
            fix_job = pending[0]
            fix_outcome = handler.run(conn, fix_job)
            with transaction(conn, immediate=True):
                handler.persist(conn, fix_job, fix_outcome)
            JobRepository(conn).update(fix_job["id"], status="succeeded")

        strategy = StrategyRepository(conn).get(strategy_id)
        assert strategy["quarantined"] == 1
        assert strategy["status"] == "quarantined"

        final_experiment = ExperimentRepository(conn).get(new_experiment_id)
        assert final_experiment["status"] == "failed"
        assert final_experiment["failure_reason"] == "code_error"

        assert JobRepository(conn).find(job_type="FIX_CODE", status="pending") == []
    finally:
        monkeypatch.delenv("AQRL_MAX_FIX_ATTEMPTS", raising=False)
        reset_settings_cache()


def test_fix_code_dedupe_key_differs_per_attempt(conn, strategy_id, spec_id):
    """Without a per-attempt dedupe key, `emit`'s default
    (`event:experiment_id`) would collapse every `CODE_CHECKS_FAILED` for one
    experiment into a single job forever — the second FIX_CODE would never
    be enqueued at all."""
    experiment, plan_id = _bar_failed_experiment(conn, strategy_id, spec_id)
    counter = iter(range(1000))
    handler.set_session(StubSession(lambda brief: _bad_spec(next(counter))))

    with transaction(conn, immediate=True):
        job_id = emit(
            conn, Event.REVIEW_ITERATE, strategy_id=strategy_id, experiment_id=experiment["id"],
            payload={"research_plan_id": plan_id, "asset_class": ASSET_CLASS},
        )
    job = JobRepository(conn).get(job_id)
    outcome = handler.run(conn, job)
    with transaction(conn, immediate=True):
        handler.persist(conn, job, outcome)

    fix_job_1 = JobRepository(conn).find(job_type="FIX_CODE")[0]
    fix_outcome_1 = handler.run(conn, fix_job_1)
    assert fix_outcome_1.checks_passed is False
    with transaction(conn, immediate=True):
        handler.persist(conn, fix_job_1, fix_outcome_1)

    fix_jobs = JobRepository(conn).find(job_type="FIX_CODE", order_by="id")
    assert len(fix_jobs) == 2
    assert fix_jobs[0]["dedupe_key"] != fix_jobs[1]["dedupe_key"]


# -- DuplicateSpecError: an exact repeat is a finding, not a bug ----------------


def test_duplicate_spec_on_plan_iteration_rolls_back_cleanly(conn, strategy_id, spec_id):
    """An LLM proposal identical to an already-tried spec is rejected before
    compute is spent (App-Flow §3.4) — `persist()` raises inside the
    transaction, and nothing partial survives the rollback."""
    experiment, plan_id = _bar_failed_experiment(conn, strategy_id, spec_id)
    # Propose the EXACT prior spec back — guaranteed spec_hash collision.
    duplicate = ProposedSpec(
        entry_logic=[node.model_dump(mode="json") for node in crossover_spec().entry_logic],
        hypothesis=crossover_spec().hypothesis,
        change_summary="an exact repeat",
    )
    handler.set_session(StubSession(duplicate))

    with transaction(conn, immediate=True):
        job_id = emit(
            conn, Event.REVIEW_ITERATE, strategy_id=strategy_id, experiment_id=experiment["id"],
            payload={"research_plan_id": plan_id, "asset_class": ASSET_CLASS},
        )
    job = JobRepository(conn).get(job_id)
    outcome = handler.run(conn, job)

    experiments_before = ExperimentRepository(conn).find(strategy_id=strategy_id)
    with pytest.raises(DuplicateSpecError):
        with transaction(conn, immediate=True):
            handler.persist(conn, job, outcome)

    experiments_after = ExperimentRepository(conn).find(strategy_id=strategy_id)
    assert len(experiments_after) == len(experiments_before), "the rollback must leave no new experiment"
